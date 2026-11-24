from app.services.diff_parser import parse_unified_diff, should_ignore_file


def test_ignore_rules():
    assert should_ignore_file("package-lock.json")[0] is True
    assert should_ignore_file("poetry.lock")[0] is True
    assert should_ignore_file("app/images/hero.png")[0] is True
    assert should_ignore_file("assets/icon.svg")[0] is True
    assert should_ignore_file("build/main.pyc")[0] is True
    assert should_ignore_file("bundle.min.js")[0] is True

    # Code files should NOT be ignored
    assert should_ignore_file("app/main.py")[0] is False
    assert should_ignore_file("src/auth.ts")[0] is False
    assert should_ignore_file("api/routes.go")[0] is False


def test_parse_unified_diff_basic():
    diff_sample = """diff --git a/app/routes.py b/app/routes.py
index abc1234..def5678 100644
--- a/app/routes.py
+++ b/app/routes.py
@@ -10,3 +10,5 @@ def get_user(user_id: int):
     return {"user_id": user_id}
+
+def delete_user(user_id: int):
+    return {"deleted": True}
"""
    files = parse_unified_diff(diff_sample)
    assert len(files) == 1
    file_diff = files[0]
    assert file_diff.filename == "app/routes.py"
    assert file_diff.is_ignored is False
    assert len(file_diff.hunks) == 1

    hunk = file_diff.hunks[0]
    assert hunk.new_start == 10
    # Added lines should be mapped with line numbers
    added_lines = hunk.added_lines
    assert len(added_lines) == 3
    # Check line numbers and contents
    assert added_lines[0][0] == 11  # line 10 is context, line 11 is empty added line
    assert added_lines[1][0] == 12
    assert "def delete_user" in added_lines[1][1]


def test_parse_unified_diff_multiple_files_with_ignored():
    diff_sample = """diff --git a/package-lock.json b/package-lock.json
index 1111111..2222222 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -1,3 +1,3 @@
 {
-  "version": "1.0.0"
+  "version": "1.0.1"
 }
diff --git a/src/service.py b/src/service.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/src/service.py
@@ -0,0 +1,2 @@
+def compute():
+    return 42
"""
    files = parse_unified_diff(diff_sample)
    assert len(files) == 2
    assert files[0].filename == "package-lock.json"
    assert files[0].is_ignored is True
    assert files[1].filename == "src/service.py"
    assert files[1].is_ignored is False
    assert files[1].is_new is True
