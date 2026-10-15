
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None


MANIFESTS = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
)

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
}

IMPORT_SKIP = {
    "__future__",
    "abc",
    "argparse",
    "ast",
    "collections",
    "contextlib",
    "dataclasses",
    "datetime",
    "enum",
    "functools",
    "importlib",
    "io",
    "itertools",
    "json",
    "logging",
    "math",
    "os",
    "pathlib",
    "re",
    "shutil",
    "subprocess",
    "sys",
    "tempfile",
    "textwrap",
    "threading",
    "time",
    "tomllib",
    "typing",
    "unittest",
    "urllib",
    "uuid",
    "warnings",
}


def normalize_name(name: str) -> str:
    """Normalize package names according to Python packaging conventions."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement(line: str) -> tuple[str, str] | None:
    """Parse a simple requirement line into package name and constraint."""
    line = line.strip()

    if not line or line.startswith("#"):
        return None

    if line.startswith(("-e ", "--editable ")):
        line = line.split(maxsplit=1)[1]

    if line.startswith(("-r ", "--requirement ")):
        return None

    if line.startswith(("--index-url", "--extra-index-url",
                        "--trusted-host", "--find-links")):
        return None

    # Remove inline comments when preceded by whitespace.
    line = re.split(r"\s+#", line, maxsplit=1)[0].strip()

    # Remove environment markers. They are retained as limitations
    # rather than interpreted as a complete dependency solver.
    line = line.split(";", maxsplit=1)[0].strip()

    # Handle common direct URL requirements.
    if " @ " in line:
        name = line.split(" @ ", maxsplit=1)[0].strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            return normalize_name(name), "direct-url"
        return None

    match = re.match(
        r"^([A-Za-z0-9][A-Za-z0-9._-]*)"
        r"(\[[^\]]+\])?"
        r"\s*(.*)$",
        line,
    )

    if not match:
        return None

    name, _extras, constraint = match.groups()
    return normalize_name(name), constraint.strip() or "(unconstrained)"


def read_requirements(
    path: Path,
    root: Path,
    declarations: dict[str, list[dict[str, Any]]],
    limitations: list[str],
) -> None:
    """Read a requirements-style file and record declaration locations."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        limitations.append(f"Could not read {path}: {exc}")
        return

    for line_number, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()

        if stripped.startswith(("-r ", "--requirement ")):
            included = stripped.split(maxsplit=1)[1].strip()
            included_path = (path.parent / included).resolve()

            try:
                included_path.relative_to(root.resolve())
            except ValueError:
                limitations.append(
                    f"Skipped requirement include outside repository: "
                    f"{included}"
                )
                continue

            if included_path.is_file():
                read_requirements(
                    included_path, root, declarations, limitations
                )
            else:
                limitations.append(
                    f"Requirement include not found: {included}"
                )
            continue

        parsed = parse_requirement(raw_line)
        if parsed is None:
            continue

        name, constraint = parsed
        declarations.setdefault(name, []).append({
            "file": str(path.relative_to(root)).replace("\\", "/"),
            "line": line_number,
            "constraint": constraint,
            "source": "requirements",
        })


def read_pyproject(
    path: Path,
    root: Path,
    declarations: dict[str, list[dict[str, Any]]],
    limitations: list[str],
) -> None:
    """Read dependency declarations from pyproject.toml."""
    if tomllib is None:
        limitations.append(
            "Cannot parse pyproject.toml: install tomli on Python 3.10, "
            "or use Python 3.11+."
        )
        return

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        limitations.append(f"Could not parse {path}: {exc}")
        return

    project = data.get("project", {})
    requirements: list[str] = []

    requirements.extend(project.get("dependencies", []))

    optional = project.get("optional-dependencies", {})
    if isinstance(optional, dict):
        for values in optional.values():
            if isinstance(values, list):
                requirements.extend(values)

    # Support common Poetry dependency declarations.
    poetry = data.get("tool", {}).get("poetry", {})
    poetry_deps = poetry.get("dependencies", {})
    if isinstance(poetry_deps, dict):
        for package, value in poetry_deps.items():
            if package.lower() == "python":
                continue

            if isinstance(value, str):
                requirements.append(f"{package}{value}")
            elif isinstance(value, dict):
                version = value.get("version", "")
                requirements.append(f"{package}{version}")

    relative_path = str(path.relative_to(root)).replace("\\", "/")

    for requirement in requirements:
        parsed = parse_requirement(requirement)
        if parsed is None:
            continue

        name, constraint = parsed
        declarations.setdefault(name, []).append({
            "file": relative_path,
            "line": None,
            "constraint": constraint,
            "source": "pyproject",
        })


def collect_declarations(
    root: Path,
    limitations: list[str],
) -> dict[str, list[dict[str, Any]]]:
    declarations: dict[str, list[dict[str, Any]]] = {}

    for manifest in MANIFESTS:
        path = root / manifest
        if not path.is_file():
            continue

        if manifest == "pyproject.toml":
            read_pyproject(path, root, declarations, limitations)
        else:
            read_requirements(path, root, declarations, limitations)

    return declarations


def scan_imports(
    root: Path,
    limitations: list[str],
) -> list[dict[str, Any]]:
    """Map statically detectable Python imports to their source locations."""
    usages: list[dict[str, Any]] = []

    for path in root.rglob("*.py"):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue

        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue

        try:
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            limitations.append(
                f"Could not analyze Python file {relative}: {exc}"
            )
            continue

        for node in ast.walk(tree):
            module = None

            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    usages.append({
                        "file": str(relative).replace("\\", "/"),
                        "line": node.lineno,
                        "module": module,
                    })

            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    module = node.module
                    usages.append({
                        "file": str(relative).replace("\\", "/"),
                        "line": node.lineno,
                        "module": module,
                    })

    usages.sort(key=lambda item: (item["file"], item["line"], item["module"]))
    return usages


def analyze(root: Path) -> dict[str, Any]:
    """Analyze a repository and return a structured evidence report."""
    root = root.resolve()

    if not root.is_dir():
        raise NotADirectoryError(f"Repository path does not exist: {root}")

    limitations: list[str] = []
    declarations = collect_declarations(root, limitations)
    import_usages = scan_imports(root, limitations)

    conflicts: list[dict[str, Any]] = []
    manifest_risks: list[dict[str, Any]] = []

    for package, entries in sorted(declarations.items()):
        constraints = {entry["constraint"] for entry in entries}

        if len(entries) > 1:
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

        for entry in entries:
            constraint = entry["constraint"]

            # Exact pins are review candidates, not automatic proof
            # of a vulnerability or incompatibility.
            if re.match(r"^==\s*[^=]", constraint):
                manifest_risks.append({
                    "package": package,
                    "constraint": constraint,
                    "file": entry["file"],
                    "line": entry["line"],
                    "risk": (
                        "Exact version pin; compatibility requires "
                        "verification"
                    ),
                    "verified_incompatibility": False,
                })

    declared_names = set(declarations)
    imported_top_level = {
        item["module"].split(".", maxsplit=1)[0].lower()
        for item in import_usages
    }

    declared_import_matches = sorted(
        declared_names.intersection(imported_top_level)
    )

    return {
        "schema_version": "1.0",
        "repository": str(root),
        "dependencies": declarations,
        "declaration_conflicts": conflicts,
        "manifest_risks": manifest_risks,
        "import_usages": import_usages,
        "declared_import_name_matches": declared_import_matches,
        "environment_check": {
            "executed": False,
            "note": (
                "No installed-environment compatibility check was run "
                "by this scanner."
            ),
        },
        "limitations": [
            *limitations,
            "Import names do not always match distribution names.",
            "Dynamic imports may not be detected.",
            "Import-to-package matching is heuristic.",
            "Environment markers and dependency resolution are not fully "
            "evaluated.",
            "Declarations alone do not prove runtime incompatibility.",
            "This scanner does not install or execute replacement packages.",
            "Exact pins are flagged for review, not automatically classified "
            "as vulnerabilities.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan Python dependency manifests and source imports."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Repository root to analyze (default: current directory).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="JSON output path (default: evidence/dependency-scan.json).",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    output = (
        Path(args.output).resolve()
        if args.output
        else root / "evidence" / "dependency-scan.json"
    )

    try:
        report = analyze(root)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except (OSError, NotADirectoryError, ValueError) as exc:
        print(f"Scanner error: {exc}", file=sys.stderr)
        return 1

    print(f"Dependency scan completed: {root}")
    print(f"Dependencies declared: {len(report['dependencies'])}")
    print(f"Declaration conflicts: {len(report['declaration_conflicts'])}")
    print(f"Manifest risks: {len(report['manifest_risks'])}")
    print(f"Import usages: {len(report['import_usages'])}")
    print(f"Report written to: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
