from app.models.schemas import FindingCategory, FindingSeverity
from app.services.diff_parser import parse_unified_diff
from app.services.static_analyzer import StaticAnalyzer


def test_static_secret_detection():
    diff_sample = """diff --git a/config.py b/config.py
index 0000000..1111111 100644
--- /dev/null
+++ b/config.py
@@ -0,0 +1,3 @@
+AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
+GITHUB_TOKEN = "ghp_1234567890abcdefghijklmnopqrstuvwxyz"
+SAFE_VAR = "hello_world"
"""
    files = parse_unified_diff(diff_sample)
    analyzer = StaticAnalyzer(enable_ruff=False)
    findings = analyzer.analyze_diff(files[0])

    assert len(findings) >= 2
    severities = [f.severity for f in findings]
    assert FindingSeverity.CRITICAL in severities

    issues = " ".join([f.issue for f in findings])
    assert "AWS Access Key ID" in issues
    assert "GitHub Personal Access Token" in issues


def test_static_python_security_flaws():
    diff_sample = """diff --git a/app/vuln.py b/app/vuln.py
index 0000000..2222222 100644
--- /dev/null
+++ b/app/vuln.py
@@ -0,0 +1,10 @@
+import subprocess
+
+def execute_cmd(user_input):
+    subprocess.run("ls " + user_input, shell=True)
+    eval(user_input)
+    try:
+        do_something()
+    except:
+        pass
+"""
    files = parse_unified_diff(diff_sample)
    analyzer = StaticAnalyzer(enable_ruff=False)
    findings = analyzer.analyze_diff(files[0])

    issues = " ".join([f.issue for f in findings])
    assert "shell=True" in issues
    assert "eval" in issues
    assert "Bare `except:`" in issues

    # Validate categories
    categories = [f.category for f in findings]
    assert FindingCategory.SECURITY in categories
    assert FindingCategory.BUG in categories

