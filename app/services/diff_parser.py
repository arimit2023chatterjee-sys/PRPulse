import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Patterns of files that should be ignored during code quality reviews
IGNORED_EXTENSIONS = {
    # Lockfiles
    ".lock",
    # Images & Media
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".bmp",
    ".tiff",
    ".pdf",
    ".mp4",
    ".mp3",
    # Binaries & Compiled
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
    ".class",
    ".exe",
    ".bin",
    ".wasm",
    # Minified assets & maps
    ".min.js",
    ".min.css",
    ".map",
}

IGNORED_FILENAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "cargo.lock",
    "go.sum",
    "pipfile.lock",
    "composer.lock",
    ".ds_store",
}


@dataclass
class DiffHunk:
    header: str
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: List[str] = field(default_factory=list)
    # Mapping of target file line numbers to added line text
    added_lines: List[Tuple[int, str]] = field(default_factory=list)


@dataclass
class ParsedFileDiff:
    filename: str
    old_path: Optional[str] = None
    new_path: Optional[str] = None
    is_deleted: bool = False
    is_new: bool = False
    is_binary: bool = False
    is_ignored: bool = False
    ignore_reason: Optional[str] = None
    hunks: List[DiffHunk] = field(default_factory=list)
    raw_diff: str = ""

    @property
    def extension(self) -> str:
        name = self.filename.lower()
        idx = name.rfind(".")
        return name[idx:] if idx != -1 else ""

    def get_added_code_text(self) -> str:
        """Concatenates all added lines for static analysis."""
        lines = []
        for hunk in self.hunks:
            for _, content in hunk.added_lines:
                lines.append(content)
        return "\n".join(lines)


def should_ignore_file(path: str) -> Tuple[bool, Optional[str]]:
    """Determine whether file should be excluded from review."""
    lower_path = path.lower().strip()
    base_name = lower_path.split("/")[-1]

    if base_name in IGNORED_FILENAMES:
        return True, f"Ignored lockfile/metadata: {base_name}"

    for ext in IGNORED_EXTENSIONS:
        if lower_path.endswith(ext):
            return True, f"Ignored binary/generated asset extension: {ext}"

    # Also ignore test snapshots or vendor directories if desired
    if "/vendor/" in lower_path or "/node_modules/" in lower_path or "/.venv/" in lower_path:
        return True, "Ignored third-party vendor directory"

    return False, None


HUNK_HEADER_REGEX = re.compile(
    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)$"
)


def parse_unified_diff(diff_text: str) -> List[ParsedFileDiff]:
    """
    Parses a standard Git unified diff into structured ParsedFileDiff objects.
    Accurately maps added lines to their 1-indexed target line numbers.
    """
    if not diff_text or not diff_text.strip():
        return []

    files: List[ParsedFileDiff] = []
    current_file: Optional[ParsedFileDiff] = None
    current_hunk: Optional[DiffHunk] = None
    current_new_line_num = 0

    lines = diff_text.splitlines()
    raw_file_lines: List[str] = []

    def finalize_current_file():
        nonlocal current_file, current_hunk, raw_file_lines
        if current_file:
            if current_hunk:
                current_file.hunks.append(current_hunk)
                current_hunk = None
            current_file.raw_diff = "\n".join(raw_file_lines)
            files.append(current_file)
            current_file = None
            raw_file_lines = []

    for line in lines:
        if line.startswith("diff --git "):
            finalize_current_file()
            # Format: diff --git a/path/to/file b/path/to/file
            parts = line.split(" ")
            old_path = parts[2][2:] if len(parts) > 2 and parts[2].startswith("a/") else ""
            new_path = parts[3][2:] if len(parts) > 3 and parts[3].startswith("b/") else ""
            filename = new_path or old_path or "unknown"

            ignored, reason = should_ignore_file(filename)
            current_file = ParsedFileDiff(
                filename=filename,
                old_path=old_path,
                new_path=new_path,
                is_ignored=ignored,
                ignore_reason=reason,
            )
            raw_file_lines = [line]
            continue

        if current_file is None:
            # Preamble or header before first diff --git
            continue

        raw_file_lines.append(line)

        if line.startswith("Binary files ") or "GIT binary patch" in line:
            current_file.is_binary = True
            current_file.is_ignored = True
            current_file.ignore_reason = "Binary file diff"
            continue

        if line.startswith("new file mode "):
            current_file.is_new = True
            continue

        if line.startswith("deleted file mode "):
            current_file.is_deleted = True
            continue

        match = HUNK_HEADER_REGEX.match(line)
        if match:
            if current_hunk:
                current_file.hunks.append(current_hunk)

            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) else 1

            current_new_line_num = new_start
            current_hunk = DiffHunk(
                header=line,
                old_start=old_start,
                old_lines=old_count,
                new_start=new_start,
                new_lines=new_count,
            )
            continue

        if current_hunk:
            current_hunk.lines.append(line)
            if line.startswith("+") and not line.startswith("+++"):
                # Line added in target file
                content = line[1:]
                current_hunk.added_lines.append((current_new_line_num, content))
                current_new_line_num += 1
            elif line.startswith("-") and not line.startswith("---"):
                # Line removed (does not increment new line num)
                pass
            else:
                # Context line (starts with space or empty)
                current_new_line_num += 1

    finalize_current_file()
    return files

