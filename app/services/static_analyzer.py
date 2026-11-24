import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import List, Optional, Tuple

from app.config import settings
from app.models.schemas import FindingCategory, FindingSeverity, ReviewFinding
from app.services.diff_parser import ParsedFileDiff

# High-confidence secret detection regexes
SECRET_PATTERNS: List[Tuple[str, re.Pattern, FindingSeverity, str]] = [
    (
        "AWS Access Key ID",
        re.compile(r"(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}"),
        FindingSeverity.CRITICAL,
        "Remove exposed AWS credential immediately. Store in environment variables or AWS Secrets Manager.",
    ),
    (
        "GitHub Personal Access Token",
        re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,82}"),
        FindingSeverity.CRITICAL,
        "Revoke and rotate this exposed GitHub token. Use GitHub Secrets instead.",
    ),
    (
        "Slack Token",
        re.compile(r"xox[baprs]-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24,32}"),
        FindingSeverity.CRITICAL,
        "Remove exposed Slack token and revoke it via the Slack developer dashboard.",
    ),
    (
        "Private Cryptographic Key",
        re.compile(r"-----BEGIN\s+(?:RSA|OPENSSH|DSA|EC|PGP)?\s*PRIVATE KEY-----"),
        FindingSeverity.CRITICAL,
        "Never commit private keys to source control. Use a secrets vault or file injection.",
    ),
    (
        "Hardcoded Secret / Password Assignment",
        re.compile(
            r"""(?i)(?:api_key|apikey|secret_key|app_secret|password|db_pass)\s*=\s*['"][^'"]{8,}['"]"""
        ),
        FindingSeverity.ERROR,
        "Do not hardcode secrets or credentials directly in code. Read from environment or config.",
    ),
    (
        "Bearer / Auth Token in Header String",
        re.compile(r"(?i)bearer\s+[a-zA-Z0-9_\-\.]{35,}"),
        FindingSeverity.ERROR,
        "Avoid hardcoded bearer authorization tokens. Inject dynamically at runtime.",
    ),
]


class StaticAnalyzer:
    """Fast deterministic static code analysis pass."""

    def __init__(self, enable_ruff: bool = True):
        self.enable_ruff = enable_ruff and bool(shutil.which("ruff"))

    def analyze_diff(self, file_diff: ParsedFileDiff) -> List[ReviewFinding]:
        findings: List[ReviewFinding] = []

        if file_diff.is_ignored or file_diff.is_deleted or file_diff.is_binary:
            return findings

        # 1. Regex-based Secrets & Credentials Detection (Across all file types)
        findings.extend(self._scan_for_secrets(file_diff))

        # 2. Language-Specific AST Static Analysis (Python)
        if file_diff.filename.endswith(".py"):
            findings.extend(self._analyze_python_ast(file_diff))

            # 3. Optional Ruff Static Linter Pass
            if self.enable_ruff:
                findings.extend(self._run_ruff_check(file_diff))

        return findings

    def _scan_for_secrets(self, file_diff: ParsedFileDiff) -> List[ReviewFinding]:
        findings: List[ReviewFinding] = []
        for hunk in file_diff.hunks:
            for line_no, content in hunk.added_lines:
                # Skip commented examples in docs or test mocks if explicit
                if "example" in content.lower() and "placeholder" in content.lower():
                    continue

                for label, pattern, severity, fix in SECRET_PATTERNS:
                    if pattern.search(content):
                        findings.append(
                            ReviewFinding(
                                file=file_diff.filename,
                                line_hint=line_no,
                                severity=severity,
                                category=FindingCategory.SECURITY,
                                issue=f"Potential {label} detected in source code.",
                                suggested_fix=fix,
                                source="static",
                            )
                        )
        return findings

    def _analyze_python_ast(self, file_diff: ParsedFileDiff) -> List[ReviewFinding]:
        """Performs Python AST inspection on modified segments and expressions."""
        findings: List[ReviewFinding] = []
        added_text = file_diff.get_added_code_text()
        if not added_text.strip():
            return findings

        # Map line numbers from diff if possible
        line_lookup = {content.strip(): line_no for hunk in file_diff.hunks for line_no, content in hunk.added_lines}

        # Check line-by-line syntax heuristics first in case partial diff is not a complete module
        for hunk in file_diff.hunks:
            for line_no, content in hunk.added_lines:
                stripped = content.strip()

                # Dangerous subprocess execution with shell=True
                if "subprocess." in stripped and "shell=True" in stripped:
                    findings.append(
                        ReviewFinding(
                            file=file_diff.filename,
                            line_hint=line_no,
                            severity=FindingSeverity.CRITICAL,
                            category=FindingCategory.SECURITY,
                            issue="Dangerous invocation of subprocess with `shell=True`. High risk of Command Injection.",
                            suggested_fix="Set `shell=False` and pass arguments as a list: `subprocess.run(['cmd', arg1], shell=False)`.",
                            source="static",
                        )
                    )

                # eval() or exec() usage
                if re.search(r"\b(eval|exec)\s*\(", stripped):
                    findings.append(
                        ReviewFinding(
                            file=file_diff.filename,
                            line_hint=line_no,
                            severity=FindingSeverity.CRITICAL,
                            category=FindingCategory.SECURITY,
                            issue="Use of dynamic code evaluation (`eval` or `exec`) introduces Arbitrary Code Execution risks.",
                            suggested_fix="Replace with safe parsers such as `ast.literal_eval`, `json.loads`, or a dedicated dispatcher.",
                            source="static",
                        )
                    )

                # Bare except: catches system exits and interrupts
                if re.match(r"^except\s*:", stripped):
                    findings.append(
                        ReviewFinding(
                            file=file_diff.filename,
                            line_hint=line_no,
                            severity=FindingSeverity.WARNING,
                            category=FindingCategory.BUG,
                            issue="Bare `except:` block catches `KeyboardInterrupt` and `SystemExit`, masking critical bugs.",
                            suggested_fix="Catch specific exceptions: `except Exception as exc:` or specify the target error type.",
                            source="static",
                        )
                    )

                # Raw SQL formatting / injection pattern
                if re.search(r"""\.(?:execute|raw)\s*\(\s*f["'].*SELECT|INSERT|UPDATE|DELETE""", stripped, re.IGNORECASE):
                    findings.append(
                        ReviewFinding(
                            file=file_diff.filename,
                            line_hint=line_no,
                            severity=FindingSeverity.ERROR,
                            category=FindingCategory.SECURITY,
                            issue="Potential SQL injection from f-string parameter interpolation in database query execution.",
                            suggested_fix="Use parameterized queries with placeholders: `cursor.execute('SELECT ... WHERE id = :id', {'id': val})`.",
                            source="static",
                        )
                    )

                # Unsafe YAML loading
                if "yaml.load(" in stripped and "Loader=" not in stripped:
                    findings.append(
                        ReviewFinding(
                            file=file_diff.filename,
                            line_hint=line_no,
                            severity=FindingSeverity.ERROR,
                            category=FindingCategory.SECURITY,
                            issue="`yaml.load()` without SafeLoader allows arbitrary object instantiation and RCE.",
                            suggested_fix="Use `yaml.safe_load(data)` instead.",
                            source="static",
                        )
                    )

                # Insecure pickle deserialization
                if "pickle.loads(" in stripped or "pickle.load(" in stripped:
                    findings.append(
                        ReviewFinding(
                            file=file_diff.filename,
                            line_hint=line_no,
                            severity=FindingSeverity.WARNING,
                            category=FindingCategory.SECURITY,
                            issue="`pickle` deserialization of untrusted input can lead to remote code execution.",
                            suggested_fix="Use a safer serialization format like JSON or Protocol Buffers.",
                            source="static",
                        )
                    )

                # Debug mode enabled
                if re.search(r"""\bdebug\s*=\s*True\b""", stripped):
                    findings.append(
                        ReviewFinding(
                            file=file_diff.filename,
                            line_hint=line_no,
                            severity=FindingSeverity.WARNING,
                            category=FindingCategory.SECURITY,
                            issue="`debug=True` detected. Ensure debug mode is not enabled in production environments.",
                            suggested_fix="Control debug mode via an environment variable flag (e.g., `debug=os.getenv('DEBUG', 'false').lower() == 'true'`).",
                            source="static",
                        )
                    )

        # Full module AST parse attempt (if code snippet is valid syntax)
        try:
            tree = ast.parse(added_text)
            for node in ast.walk(tree):
                # Check for assert statements in non-test production code
                if isinstance(node, ast.Assert) and not file_diff.filename.startswith("test"):
                    findings.append(
                        ReviewFinding(
                            file=file_diff.filename,
                            line_hint=None,
                            severity=FindingSeverity.INFO,
                            category=FindingCategory.MAINTAINABILITY,
                            issue="`assert` statement used in production code. Asserts are removed when Python runs with `-O` optimizations.",
                            suggested_fix="Raise explicit exceptions (e.g. `raise ValueError(...)`) for runtime validation.",
                            source="static",
                        )
                    )
        except SyntaxError:
            # Code diff was a partial block, line-by-line checks already covered it
            pass

        return findings

    def _run_ruff_check(self, file_diff: ParsedFileDiff) -> List[ReviewFinding]:
        """Runs ruff on the added code contents to catch lint and syntax issues."""
        findings: List[ReviewFinding] = []
        code = file_diff.get_added_code_text()
        if not code.strip():
            return findings

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as temp_file:
            temp_path = temp_file.name
            temp_file.write(code)

        try:
            cmd = ["ruff", "check", "--output-format=json", temp_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            output = result.stdout.strip()
            if output:
                try:
                    ruff_issues = json.loads(output)
                    for item in ruff_issues[:5]:  # Cap to top 5 per file to avoid noise
                        code_rule = item.get("code", "")
                        message = item.get("message", "")
                        # Map line number if possible
                        hunk_line = None
                        if file_diff.hunks and file_diff.hunks[0].added_lines:
                            hunk_line = file_diff.hunks[0].added_lines[0][0]

                        severity = (
                            FindingSeverity.ERROR
                            if code_rule.startswith("E9") or code_rule.startswith("F821")
                            else FindingSeverity.INFO
                        )

                        category = (
                            FindingCategory.BUG
                            if code_rule.startswith("F") or code_rule.startswith("E9")
                            else FindingCategory.MAINTAINABILITY
                        )

                        findings.append(
                            ReviewFinding(
                                file=file_diff.filename,
                                line_hint=hunk_line,
                                severity=severity,
                                category=category,
                                issue=f"[Ruff {code_rule}] {message}",
                                suggested_fix=f"Resolve ruff lint code {code_rule}: {message}",
                                source="static",
                            )
                        )
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

        return findings

