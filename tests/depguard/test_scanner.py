
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCANNER = PROJECT_ROOT / "depguard" / "scanner.py"


def test_scanner_runs_against_repository(tmp_path):
    output = tmp_path / "scan.json"

    result = subprocess.run(
        [
            sys.executable,
            str(SCANNER),
            str(PROJECT_ROOT),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert output.exists()

    report = json.loads(output.read_text(encoding="utf-8"))
    assert "dependencies" in report
    assert "import_usages" in report
    assert "declaration_conflicts" in report


def test_scanner_detects_different_constraints(tmp_path):
    from depguard.scanner import analyze

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\n'
        'dependencies = ["demo-package>=2"]\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text(
        "demo_package==1.0\n",
        encoding="utf-8",
    )

    report = analyze(tmp_path)
    package = report["dependencies"]["demo-package"]

    assert len(package) == 2
    assert any(
        item["different_constraints"]
        for item in report["declaration_conflicts"]
    )
