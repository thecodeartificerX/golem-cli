from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from golem.config import GolemConfig


# ---------------------------------------------------------------------------
# Regex patterns for Python source analysis
# ---------------------------------------------------------------------------

_PY_IMPORT_RE = re.compile(r"^(?:from\s+\S+\s+)?import\s+", re.MULTILINE)
_PY_FUNC_RE = re.compile(r"^def\s+(\w+)\s*\(", re.MULTILINE)
_PY_CLASS_RE = re.compile(r"^class\s+(\w+)\s*[:(]", re.MULTILINE)
_PY_METHOD_RE = re.compile(r"^    def\s+(\w+)\s*\(", re.MULTILINE)
_PY_DECORATOR_RE = re.compile(r"^@(\w[\w.]*)", re.MULTILINE)

_CONFIG_EXTS = {".json", ".toml", ".yaml", ".yml"}

# ---------------------------------------------------------------------------
# Core enums
# ---------------------------------------------------------------------------


class ChangeType(str, Enum):
    # Imports
    ADD_IMPORT = "add_import"
    REMOVE_IMPORT = "remove_import"
    MODIFY_IMPORT = "modify_import"
    # Functions
    ADD_FUNCTION = "add_function"
    REMOVE_FUNCTION = "remove_function"
    MODIFY_FUNCTION = "modify_function"
    # Classes and methods
    ADD_CLASS = "add_class"
    MODIFY_CLASS = "modify_class"
    ADD_METHOD = "add_method"
    MODIFY_METHOD = "modify_method"
    # Variables and constants
    ADD_VARIABLE = "add_variable"
    MODIFY_VARIABLE = "modify_variable"
    ADD_CONSTANT = "add_constant"
    # Python-specific
    ADD_DECORATOR = "add_decorator"
    REMOVE_DECORATOR = "remove_decorator"
    # Config files (JSON/TOML/YAML)
    ADD_CONFIG_KEY = "add_config_key"
    MODIFY_CONFIG_KEY = "modify_config_key"
    # Generic
    ADD_COMMENT = "add_comment"
    FORMATTING_ONLY = "formatting_only"
    UNKNOWN = "unknown"


class MergeStrategy(str, Enum):
    COMBINE_IMPORTS = "combine_imports"
    APPEND_FUNCTIONS = "append_functions"
    APPEND_METHODS = "append_methods"
    ORDER_BY_DEPENDENCY = "order_by_dependency"
    COMBINE_CONFIGS = "combine_configs"
    AI_REQUIRED = "ai_required"
    HUMAN_REQUIRED = "human_required"


class MergeDecision(str, Enum):
    AUTO_MERGED = "auto_merged"
    AI_MERGED = "ai_merged"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    FAILED = "failed"
    DIRECT_COPY = "direct_copy"


class ConflictSeverity(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SemanticChange:
    change_type: ChangeType
    target: str           # function name, import string, class name, etc.
    location: str         # grouping key: "file_top", "function:name", "class:name"
    line_start: int
    line_end: int
    content_before: str = ""
    content_after: str = ""


@dataclass
class FileAnalysis:
    file_path: str
    changes: list[SemanticChange] = field(default_factory=list)
    functions_added: set[str] = field(default_factory=set)
    functions_modified: set[str] = field(default_factory=set)
    imports_added: set[str] = field(default_factory=set)
    imports_removed: set[str] = field(default_factory=set)
    classes_modified: set[str] = field(default_factory=set)
    total_lines_changed: int = 0


@dataclass
class ConflictRegion:
    file_path: str
    location: str
    branches_involved: list[str]
    change_types: list[ChangeType]
    severity: ConflictSeverity
    can_auto_merge: bool
    merge_strategy: MergeStrategy | None
    reason: str


@dataclass
class FileMergeResult:
    decision: MergeDecision
    file_path: str
    merged_content: str | None
    conflicts_resolved: list[ConflictRegion] = field(default_factory=list)
    conflicts_remaining: list[ConflictRegion] = field(default_factory=list)
    ai_calls_made: int = 0
    explanation: str = ""
    error: str = ""


@dataclass
class MergeStats:
    files_processed: int = 0
    files_auto_merged: int = 0
    files_ai_merged: int = 0
    files_need_review: int = 0
    files_failed: int = 0
    conflicts_detected: int = 0
    conflicts_auto_resolved: int = 0
    conflicts_ai_resolved: int = 0
    ai_calls_made: int = 0
    duration_ms: int = 0


@dataclass
class MergeReport:
    success: bool
    branches_merged: list[str]
    file_results: dict[str, FileMergeResult]
    stats: MergeStats
    error: str = ""


# ---------------------------------------------------------------------------
# Semantic Analyzer
# ---------------------------------------------------------------------------


def _extract_function_body(content: str, func_name: str) -> str:
    """
    Extract the full def block for `func_name` from Python source.
    Uses indentation depth: body ends when we return to the same indentation
    as the `def` line (or reach another top-level def/class).
    Returns the raw text including the signature line, or "" if not found.
    """
    lines = content.splitlines(keepends=True)
    pattern = re.compile(r"^(\s*)def\s+" + re.escape(func_name) + r"\s*\(")
    start_idx = -1
    indent = ""
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if m:
            start_idx = i
            indent = m.group(1)
            break
    if start_idx == -1:
        return ""

    body_lines = [lines[start_idx]]
    for line in lines[start_idx + 1:]:
        stripped = line.rstrip("\n").rstrip("\r")
        if stripped == "":
            body_lines.append(line)
            continue
        # If line starts with the same or less indentation as def and is non-blank
        # and is another top-level construct, stop
        if not stripped.startswith(indent + " ") and not stripped.startswith(indent + "\t"):
            if indent == "" and (stripped.startswith("def ") or stripped.startswith("class ")
                                  or stripped.startswith("@")):
                break
            elif indent != "" and len(stripped) - len(stripped.lstrip()) <= len(indent):
                break
        body_lines.append(line)

    # Strip trailing blank lines so body comparison is stable regardless of
    # what follows the function in the file.
    while body_lines and body_lines[-1].strip() == "":
        body_lines.pop()

    return "".join(body_lines) + "\n" if body_lines else ""


def _extract_methods_for_class(content: str, class_name: str) -> set[str]:
    """Extract the set of method names defined in `class_name`."""
    lines = content.splitlines()
    in_class = False
    class_indent = ""
    methods: set[str] = set()
    class_pattern = re.compile(r"^(\s*)class\s+" + re.escape(class_name) + r"\s*[:(]")
    method_pattern = re.compile(r"^(\s+)def\s+(\w+)\s*\(")

    for line in lines:
        cm = class_pattern.match(line)
        if cm:
            in_class = True
            class_indent = cm.group(1)
            continue
        if in_class:
            stripped = line.rstrip()
            if stripped == "":
                continue
            # Check if we left the class (back to same or lower indentation)
            leading = len(line) - len(line.lstrip())
            class_indent_len = len(class_indent)
            if leading <= class_indent_len and stripped and not stripped[leading:].startswith("#"):
                # Non-blank, non-comment line at class level or above — class ended
                in_class = False
                continue
            mm = method_pattern.match(line)
            if mm:
                method_indent = mm.group(1)
                # Only direct methods (one extra indent level from class)
                if len(method_indent) == class_indent_len + 4 or (
                    class_indent_len == 0 and len(method_indent) == 4
                ):
                    methods.add(mm.group(2))
    return methods


def _analyze_config_diff(file_path: str, before: str, after: str) -> FileAnalysis:
    """
    For JSON/TOML/YAML files: parse both sides, enumerate key additions/modifications.
    Falls back to line-level analysis if parsing fails.
    """
    analysis = FileAnalysis(file_path=file_path)
    ext = Path(file_path).suffix.lower()

    def _flatten(d: object, prefix: str = "") -> dict[str, object]:
        result: dict[str, object] = {}
        if isinstance(d, dict):
            for k, v in d.items():
                key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    result.update(_flatten(v, key))
                else:
                    result[key] = v
        return result

    if ext == ".json":
        try:
            before_data = json.loads(before) if before.strip() else {}
            after_data = json.loads(after) if after.strip() else {}
        except json.JSONDecodeError:
            return analysis

        before_flat = _flatten(before_data)
        after_flat = _flatten(after_data)

        for key in after_flat:
            if key not in before_flat:
                analysis.changes.append(
                    SemanticChange(
                        change_type=ChangeType.ADD_CONFIG_KEY,
                        target=key,
                        location="config_root",
                        line_start=1,
                        line_end=1,
                        content_after=json.dumps(after_data, indent=2, ensure_ascii=False),
                    )
                )
            elif before_flat[key] != after_flat[key]:
                analysis.changes.append(
                    SemanticChange(
                        change_type=ChangeType.MODIFY_CONFIG_KEY,
                        target=key,
                        location="config_root",
                        line_start=1,
                        line_end=1,
                        content_before=str(before_flat[key]),
                        content_after=str(after_flat[key]),
                    )
                )
        analysis.total_lines_changed = abs(len(after.splitlines()) - len(before.splitlines()))
        return analysis

    if ext == ".toml":
        try:
            before_data_toml = tomllib.loads(before) if before.strip() else {}
            after_data_toml = tomllib.loads(after) if after.strip() else {}
        except Exception:
            return analysis

        before_flat = _flatten(before_data_toml)
        after_flat = _flatten(after_data_toml)

        for key in after_flat:
            if key not in before_flat:
                analysis.changes.append(
                    SemanticChange(
                        change_type=ChangeType.ADD_CONFIG_KEY,
                        target=key,
                        location="config_root",
                        line_start=1,
                        line_end=1,
                        content_after=str(after_flat[key]),
                    )
                )
            elif before_flat[key] != after_flat[key]:
                analysis.changes.append(
                    SemanticChange(
                        change_type=ChangeType.MODIFY_CONFIG_KEY,
                        target=key,
                        location="config_root",
                        line_start=1,
                        line_end=1,
                        content_before=str(before_flat[key]),
                        content_after=str(after_flat[key]),
                    )
                )
        analysis.total_lines_changed = abs(len(after.splitlines()) - len(before.splitlines()))
        return analysis

    # YAML / unknown: line-level fallback (treat as opaque)
    return analysis


def analyze_diff(file_path: str, before: str, after: str) -> FileAnalysis:
    """
    Classify what changed between `before` and `after` content of `file_path`.
    Returns a FileAnalysis with SemanticChange entries, one per detected change.
    """
    ext = Path(file_path).suffix.lower()

    # Delegate config files immediately
    if ext in _CONFIG_EXTS:
        return _analyze_config_diff(file_path, before, after)

    analysis = FileAnalysis(file_path=file_path)
    before_lines = before.splitlines()
    after_lines = after.splitlines()

    # ------------------------------------------------------------------
    # 1. Import detection — scan added/removed lines
    # ------------------------------------------------------------------
    before_import_set: set[str] = set()
    after_import_set: set[str] = set()

    for line in before_lines:
        if _PY_IMPORT_RE.match(line.lstrip()):
            before_import_set.add(line.strip())

    for line in after_lines:
        if _PY_IMPORT_RE.match(line.lstrip()):
            after_import_set.add(line.strip())

    for imp in after_import_set - before_import_set:
        # Find approximate line number in after
        try:
            lineno = next(i + 1 for i, l in enumerate(after_lines) if l.strip() == imp)
        except StopIteration:
            lineno = 1
        analysis.changes.append(
            SemanticChange(
                change_type=ChangeType.ADD_IMPORT,
                target=imp,
                location="file_top",
                line_start=lineno,
                line_end=lineno,
                content_after=imp,
            )
        )
        analysis.imports_added.add(imp)

    for imp in before_import_set - after_import_set:
        try:
            lineno = next(i + 1 for i, l in enumerate(before_lines) if l.strip() == imp)
        except StopIteration:
            lineno = 1
        analysis.changes.append(
            SemanticChange(
                change_type=ChangeType.REMOVE_IMPORT,
                target=imp,
                location="file_top",
                line_start=lineno,
                line_end=lineno,
                content_before=imp,
            )
        )
        analysis.imports_removed.add(imp)

    # ------------------------------------------------------------------
    # 2. Function detection — compare function name sets
    # ------------------------------------------------------------------
    before_funcs = set(_PY_FUNC_RE.findall(before))
    after_funcs = set(_PY_FUNC_RE.findall(after))

    for func in after_funcs - before_funcs:
        body = _extract_function_body(after, func)
        # Find start line
        try:
            lineno = next(i + 1 for i, l in enumerate(after_lines) if re.match(r"^def\s+" + re.escape(func) + r"\s*\(", l))
        except StopIteration:
            lineno = 1
        end_lineno = lineno + body.count("\n") if body else lineno
        analysis.changes.append(
            SemanticChange(
                change_type=ChangeType.ADD_FUNCTION,
                target=func,
                location=f"function:{func}",
                line_start=lineno,
                line_end=end_lineno,
                content_after=body,
            )
        )
        analysis.functions_added.add(func)

    for func in before_funcs - after_funcs:
        try:
            lineno = next(i + 1 for i, l in enumerate(before_lines) if re.match(r"^def\s+" + re.escape(func) + r"\s*\(", l))
        except StopIteration:
            lineno = 1
        analysis.changes.append(
            SemanticChange(
                change_type=ChangeType.REMOVE_FUNCTION,
                target=func,
                location=f"function:{func}",
                line_start=lineno,
                line_end=lineno,
            )
        )

    for func in before_funcs & after_funcs:
        body_before = _extract_function_body(before, func)
        body_after = _extract_function_body(after, func)
        if body_before != body_after:
            try:
                lineno = next(i + 1 for i, l in enumerate(after_lines) if re.match(r"^def\s+" + re.escape(func) + r"\s*\(", l))
            except StopIteration:
                lineno = 1
            analysis.changes.append(
                SemanticChange(
                    change_type=ChangeType.MODIFY_FUNCTION,
                    target=func,
                    location=f"function:{func}",
                    line_start=lineno,
                    line_end=lineno + body_after.count("\n") if body_after else lineno,
                    content_before=body_before,
                    content_after=body_after,
                )
            )
            analysis.functions_modified.add(func)

    # ------------------------------------------------------------------
    # 3. Class detection
    # ------------------------------------------------------------------
    before_classes = set(_PY_CLASS_RE.findall(before))
    after_classes = set(_PY_CLASS_RE.findall(after))

    for cls in after_classes - before_classes:
        try:
            lineno = next(i + 1 for i, l in enumerate(after_lines) if re.match(r"^class\s+" + re.escape(cls) + r"\s*[:(]", l))
        except StopIteration:
            lineno = 1
        analysis.changes.append(
            SemanticChange(
                change_type=ChangeType.ADD_CLASS,
                target=cls,
                location=f"class:{cls}",
                line_start=lineno,
                line_end=lineno,
            )
        )

    for cls in before_classes & after_classes:
        before_methods = _extract_methods_for_class(before, cls)
        after_methods = _extract_methods_for_class(after, cls)

        for method in after_methods - before_methods:
            # Find line number in after
            method_pat = re.compile(r"^\s+def\s+" + re.escape(method) + r"\s*\(")
            try:
                lineno = next(i + 1 for i, l in enumerate(after_lines) if method_pat.match(l))
            except StopIteration:
                lineno = 1
            # Extract method body (indented block)
            method_body = _extract_function_body(after, method)
            analysis.changes.append(
                SemanticChange(
                    change_type=ChangeType.ADD_METHOD,
                    target=f"{cls}.{method}",
                    location=f"class:{cls}",
                    line_start=lineno,
                    line_end=lineno + method_body.count("\n") if method_body else lineno,
                    content_after=method_body,
                )
            )

        for method in before_methods & after_methods:
            body_before = _extract_function_body(before, method)
            body_after = _extract_function_body(after, method)
            if body_before != body_after:
                method_pat = re.compile(r"^\s+def\s+" + re.escape(method) + r"\s*\(")
                try:
                    lineno = next(i + 1 for i, l in enumerate(after_lines) if method_pat.match(l))
                except StopIteration:
                    lineno = 1
                analysis.changes.append(
                    SemanticChange(
                        change_type=ChangeType.MODIFY_METHOD,
                        target=f"{cls}.{method}",
                        location=f"class:{cls}",
                        line_start=lineno,
                        line_end=lineno,
                        content_before=body_before,
                        content_after=body_after,
                    )
                )
                analysis.classes_modified.add(cls)

    # ------------------------------------------------------------------
    # 4. Decorator detection
    # ------------------------------------------------------------------
    before_decs = set(_PY_DECORATOR_RE.findall(before))
    after_decs = set(_PY_DECORATOR_RE.findall(after))

    for dec in after_decs - before_decs:
        analysis.changes.append(
            SemanticChange(
                change_type=ChangeType.ADD_DECORATOR,
                target=dec,
                location="file_top",
                line_start=1,
                line_end=1,
                content_after=f"@{dec}",
            )
        )

    for dec in before_decs - after_decs:
        analysis.changes.append(
            SemanticChange(
                change_type=ChangeType.REMOVE_DECORATOR,
                target=dec,
                location="file_top",
                line_start=1,
                line_end=1,
                content_before=f"@{dec}",
            )
        )

    # ------------------------------------------------------------------
    # Total lines changed
    # ------------------------------------------------------------------
    before_set = set(before_lines)
    after_set = set(after_lines)
    analysis.total_lines_changed = len(before_set.symmetric_difference(after_set))

    return analysis


# ---------------------------------------------------------------------------
# Conflict Detector
# ---------------------------------------------------------------------------

_RuleKey = str  # "change_type_a::change_type_b" alphabetically sorted


@dataclass
class CompatibilityRule:
    change_type_a: ChangeType
    change_type_b: ChangeType
    compatible: bool
    strategy: MergeStrategy | None
    reason: str


class ConflictDetector:
    def __init__(self) -> None:
        self._rules: dict[_RuleKey, CompatibilityRule] = {}
        self._build_default_rules()

    def _rule_key(self, a: ChangeType, b: ChangeType) -> _RuleKey:
        pair = sorted([a.value, b.value])
        return f"{pair[0]}::{pair[1]}"

    def _add_rule(
        self,
        a: ChangeType,
        b: ChangeType,
        compatible: bool,
        strategy: MergeStrategy | None,
        reason: str,
    ) -> None:
        key = self._rule_key(a, b)
        self._rules[key] = CompatibilityRule(
            change_type_a=a,
            change_type_b=b,
            compatible=compatible,
            strategy=strategy,
            reason=reason,
        )

    def _build_default_rules(self) -> None:
        """Register all compatibility rules from the spec table."""
        add = self._add_rule

        # Import rules
        add(ChangeType.ADD_IMPORT, ChangeType.ADD_IMPORT, True, MergeStrategy.COMBINE_IMPORTS,
            "Both branches add imports — combine import blocks")
        add(ChangeType.ADD_IMPORT, ChangeType.REMOVE_IMPORT, False, MergeStrategy.AI_REQUIRED,
            "One branch adds import while other removes it — semantic conflict")
        add(ChangeType.ADD_IMPORT, ChangeType.ADD_FUNCTION, True, MergeStrategy.COMBINE_IMPORTS,
            "Import addition is compatible with new function")
        add(ChangeType.ADD_IMPORT, ChangeType.MODIFY_FUNCTION, True, MergeStrategy.COMBINE_IMPORTS,
            "Import addition is compatible with function modification")
        add(ChangeType.ADD_IMPORT, ChangeType.ADD_CLASS, True, MergeStrategy.COMBINE_IMPORTS,
            "Import addition compatible with new class")
        add(ChangeType.ADD_IMPORT, ChangeType.ADD_VARIABLE, True, MergeStrategy.COMBINE_IMPORTS,
            "Import addition compatible with new variable")
        add(ChangeType.REMOVE_IMPORT, ChangeType.REMOVE_IMPORT, True, MergeStrategy.COMBINE_IMPORTS,
            "Both branches remove imports — combine removals")

        # Function rules
        add(ChangeType.ADD_FUNCTION, ChangeType.ADD_FUNCTION, True, MergeStrategy.APPEND_FUNCTIONS,
            "Both branches add new functions — append both")
        add(ChangeType.ADD_FUNCTION, ChangeType.MODIFY_FUNCTION, True, MergeStrategy.APPEND_FUNCTIONS,
            "One adds function, other modifies different function — append new")
        add(ChangeType.MODIFY_FUNCTION, ChangeType.MODIFY_FUNCTION, False, MergeStrategy.AI_REQUIRED,
            "Both branches modified the same function body — semantic conflict")
        add(ChangeType.ADD_FUNCTION, ChangeType.REMOVE_FUNCTION, False, MergeStrategy.AI_REQUIRED,
            "One branch adds, other removes a function — semantic conflict")
        add(ChangeType.REMOVE_FUNCTION, ChangeType.REMOVE_FUNCTION, True, MergeStrategy.APPEND_FUNCTIONS,
            "Both branches remove functions — compatible")
        add(ChangeType.REMOVE_FUNCTION, ChangeType.MODIFY_FUNCTION, False, MergeStrategy.AI_REQUIRED,
            "One removes, other modifies same function — semantic conflict")

        # Class and method rules
        add(ChangeType.ADD_CLASS, ChangeType.ADD_CLASS, True, MergeStrategy.APPEND_FUNCTIONS,
            "Both branches add new classes — append both")
        add(ChangeType.ADD_CLASS, ChangeType.MODIFY_CLASS, True, MergeStrategy.APPEND_FUNCTIONS,
            "One adds class, other modifies different class — compatible")
        add(ChangeType.MODIFY_CLASS, ChangeType.MODIFY_CLASS, False, MergeStrategy.AI_REQUIRED,
            "Both branches modify same class — semantic conflict")
        add(ChangeType.ADD_METHOD, ChangeType.ADD_METHOD, True, MergeStrategy.APPEND_METHODS,
            "Both branches add new methods — append both")
        add(ChangeType.ADD_METHOD, ChangeType.MODIFY_METHOD, True, MergeStrategy.APPEND_METHODS,
            "One adds method, other modifies different method — compatible")
        add(ChangeType.MODIFY_METHOD, ChangeType.MODIFY_METHOD, False, MergeStrategy.AI_REQUIRED,
            "Both branches modify same method — semantic conflict")
        add(ChangeType.ADD_METHOD, ChangeType.ADD_FUNCTION, True, MergeStrategy.APPEND_FUNCTIONS,
            "Method addition compatible with new function")

        # Variable and constant rules
        add(ChangeType.ADD_VARIABLE, ChangeType.ADD_VARIABLE, True, MergeStrategy.APPEND_FUNCTIONS,
            "Both branches add variables — append both")
        add(ChangeType.ADD_CONSTANT, ChangeType.ADD_CONSTANT, True, MergeStrategy.APPEND_FUNCTIONS,
            "Both branches add constants — append both")
        add(ChangeType.ADD_VARIABLE, ChangeType.ADD_CONSTANT, True, MergeStrategy.APPEND_FUNCTIONS,
            "Variable and constant additions are compatible")
        add(ChangeType.MODIFY_VARIABLE, ChangeType.MODIFY_VARIABLE, False, MergeStrategy.AI_REQUIRED,
            "Both branches modify same variable — semantic conflict")
        add(ChangeType.ADD_VARIABLE, ChangeType.MODIFY_VARIABLE, True, MergeStrategy.APPEND_FUNCTIONS,
            "New variable compatible with modification of different variable")
        add(ChangeType.ADD_VARIABLE, ChangeType.ADD_FUNCTION, True, MergeStrategy.APPEND_FUNCTIONS,
            "Variable addition compatible with new function")
        add(ChangeType.ADD_VARIABLE, ChangeType.MODIFY_FUNCTION, True, MergeStrategy.APPEND_FUNCTIONS,
            "Variable addition compatible with function modification")
        add(ChangeType.ADD_CONSTANT, ChangeType.ADD_FUNCTION, True, MergeStrategy.APPEND_FUNCTIONS,
            "Constant addition compatible with new function")

        # Decorator rules
        add(ChangeType.ADD_DECORATOR, ChangeType.ADD_DECORATOR, True, MergeStrategy.ORDER_BY_DEPENDENCY,
            "Both branches add decorators — order by dependency")
        add(ChangeType.ADD_DECORATOR, ChangeType.MODIFY_FUNCTION, True, MergeStrategy.ORDER_BY_DEPENDENCY,
            "Decorator addition compatible with function modification")
        add(ChangeType.ADD_DECORATOR, ChangeType.REMOVE_DECORATOR, False, MergeStrategy.AI_REQUIRED,
            "One adds decorator, other removes it — semantic conflict")
        add(ChangeType.REMOVE_DECORATOR, ChangeType.REMOVE_DECORATOR, True, MergeStrategy.ORDER_BY_DEPENDENCY,
            "Both branches remove decorators — compatible")

        # Config rules
        add(ChangeType.ADD_CONFIG_KEY, ChangeType.ADD_CONFIG_KEY, True, MergeStrategy.COMBINE_CONFIGS,
            "Both branches add config keys — combine configs")
        add(ChangeType.MODIFY_CONFIG_KEY, ChangeType.MODIFY_CONFIG_KEY, False, MergeStrategy.AI_REQUIRED,
            "Both branches modify config keys — semantic conflict")
        add(ChangeType.ADD_CONFIG_KEY, ChangeType.MODIFY_CONFIG_KEY, True, MergeStrategy.COMBINE_CONFIGS,
            "Config key addition compatible with modification of different key")

        # Comment and formatting rules
        add(ChangeType.ADD_COMMENT, ChangeType.ADD_COMMENT, True, MergeStrategy.APPEND_FUNCTIONS,
            "Both branches add comments — compatible")
        add(ChangeType.ADD_COMMENT, ChangeType.ADD_FUNCTION, True, MergeStrategy.APPEND_FUNCTIONS,
            "Comment addition compatible with new function")
        add(ChangeType.FORMATTING_ONLY, ChangeType.ADD_FUNCTION, True, MergeStrategy.APPEND_FUNCTIONS,
            "Formatting change compatible with new function")
        add(ChangeType.FORMATTING_ONLY, ChangeType.MODIFY_FUNCTION, True, MergeStrategy.APPEND_FUNCTIONS,
            "Formatting change compatible with function modification")
        add(ChangeType.FORMATTING_ONLY, ChangeType.FORMATTING_ONLY, True, MergeStrategy.ORDER_BY_DEPENDENCY,
            "Both branches only made formatting changes — compatible")

    def _assess_severity(self, change_types: list[ChangeType], compatible: bool) -> ConflictSeverity:
        """Assess conflict severity from change types."""
        if compatible:
            return ConflictSeverity.NONE

        modify_heavy = {ChangeType.MODIFY_FUNCTION, ChangeType.MODIFY_METHOD}
        high_risk = {ChangeType.REMOVE_FUNCTION}

        modify_count = sum(1 for ct in change_types if ct in modify_heavy)
        if modify_count >= 2:
            return ConflictSeverity.CRITICAL
        if any(ct in high_risk for ct in change_types):
            return ConflictSeverity.HIGH
        if any(ct in modify_heavy for ct in change_types):
            return ConflictSeverity.MEDIUM
        return ConflictSeverity.LOW

    def detect_conflicts(
        self,
        branch_analyses: dict[str, FileAnalysis],
    ) -> list[ConflictRegion]:
        """
        For each location touched by 2+ branches, check all change-type pairs.
        Returns list of ConflictRegion — one per conflicting location.
        """
        # Build location_map: location -> list[(branch, SemanticChange)]
        location_map: dict[str, list[tuple[str, SemanticChange]]] = {}
        for branch, analysis in branch_analyses.items():
            for change in analysis.changes:
                location_map.setdefault(change.location, []).append((branch, change))

        regions: list[ConflictRegion] = []

        for location, entries in location_map.items():
            # Only consider locations touched by 2+ branches
            branches_at_loc = {branch for branch, _ in entries}
            if len(branches_at_loc) < 2:
                continue

            # Cross-product all pairs from different branches
            all_compatible = True
            final_strategy: MergeStrategy | None = None
            collected_change_types: list[ChangeType] = []

            for i, (branch_a, change_a) in enumerate(entries):
                for branch_b, change_b in entries[i + 1:]:
                    if branch_a == branch_b:
                        continue
                    # Skip if targets differ (different function names at same location class)
                    # For function/class locations, different targets = additive, not conflicting
                    if location.startswith("function:") or location.startswith("class:"):
                        if change_a.target != change_b.target:
                            # Different targets at same class = additive, compatible
                            collected_change_types.extend([change_a.change_type, change_b.change_type])
                            # Use APPEND_FUNCTIONS/APPEND_METHODS as appropriate
                            if location.startswith("class:"):
                                if final_strategy is None:
                                    final_strategy = MergeStrategy.APPEND_METHODS
                            else:
                                if final_strategy is None:
                                    final_strategy = MergeStrategy.APPEND_FUNCTIONS
                            continue

                    key = self._rule_key(change_a.change_type, change_b.change_type)
                    rule = self._rules.get(key)

                    collected_change_types.extend([change_a.change_type, change_b.change_type])

                    if rule is None:
                        # No rule found — incompatible, requires AI
                        all_compatible = False
                        final_strategy = MergeStrategy.AI_REQUIRED
                    elif not rule.compatible:
                        all_compatible = False
                        final_strategy = MergeStrategy.AI_REQUIRED
                    else:
                        if final_strategy is None or all_compatible:
                            final_strategy = rule.strategy

            if not collected_change_types:
                continue

            severity = self._assess_severity(collected_change_types, all_compatible)
            reason_parts: list[str] = []
            for i, (branch_a, change_a) in enumerate(entries):
                for branch_b, change_b in entries[i + 1:]:
                    if branch_a == branch_b:
                        continue
                    key = self._rule_key(change_a.change_type, change_b.change_type)
                    rule = self._rules.get(key)
                    if rule:
                        reason_parts.append(rule.reason)
                    else:
                        reason_parts.append(f"No rule for {change_a.change_type} vs {change_b.change_type}")

            reason = "; ".join(dict.fromkeys(reason_parts))  # deduplicate preserving order

            regions.append(
                ConflictRegion(
                    file_path=branch_analyses[next(iter(branches_at_loc))].file_path,
                    location=location,
                    branches_involved=sorted(branches_at_loc),
                    change_types=list(dict.fromkeys(collected_change_types)),
                    severity=severity,
                    can_auto_merge=all_compatible,
                    merge_strategy=final_strategy,
                    reason=reason,
                )
            )

        return regions


# ---------------------------------------------------------------------------
# Auto Merger — 5 Strategies
# ---------------------------------------------------------------------------


def _find_import_section_end(lines: list[str]) -> int:
    """
    Returns the line index (exclusive) after the last import line in the import block.
    Returns 0 if no imports found.
    Handles 'import x', 'from x import y', blank separator lines between groups.
    """
    last_import_idx = -1
    found_import = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if _PY_IMPORT_RE.match(stripped):
            last_import_idx = i
            found_import = True
        elif found_import and stripped == "":
            # blank line after imports — might be part of the import block
            pass
        elif found_import and stripped.startswith("#"):
            # comment after imports — part of the block
            pass
        elif found_import and stripped and not _PY_IMPORT_RE.match(stripped):
            # non-blank, non-import, non-comment after imports started — stop
            break

    return last_import_idx + 1 if last_import_idx >= 0 else 0


def _strategy_combine_imports(
    baseline: str,
    branch_analyses: dict[str, FileAnalysis],
) -> str:
    """
    Collect all ADD_IMPORT.content_after lines from all branches.
    Deduplicate against imports already present in baseline.
    Remove any REMOVE_IMPORT.content_before lines.
    Insert new imports after the last existing import line.
    """
    lines = baseline.splitlines(keepends=True)

    # Collect all existing imports from baseline
    existing_imports: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if _PY_IMPORT_RE.match(stripped):
            existing_imports.add(stripped)

    # Gather new imports to add and imports to remove
    imports_to_add: list[str] = []
    imports_to_remove: set[str] = set()

    for analysis in branch_analyses.values():
        for change in analysis.changes:
            if change.change_type == ChangeType.ADD_IMPORT and change.content_after:
                imp = change.content_after.strip()
                if imp and imp not in existing_imports and imp not in imports_to_add:
                    imports_to_add.append(imp)
            elif change.change_type == ChangeType.REMOVE_IMPORT and change.content_before:
                imports_to_remove.add(change.content_before.strip())

    # Remove imports scheduled for removal
    result_lines = [line for line in lines if line.strip() not in imports_to_remove]

    if not imports_to_add:
        return "".join(result_lines)

    # Find insertion point
    plain_lines = [line.rstrip("\n").rstrip("\r") for line in result_lines]
    insert_pos = _find_import_section_end(plain_lines)

    # Build import lines to insert
    new_import_lines = [f"{imp}\n" for imp in imports_to_add]

    result_lines = result_lines[:insert_pos] + new_import_lines + result_lines[insert_pos:]
    return "".join(result_lines)


def _find_insert_before_main(lines: list[str]) -> int:
    """
    Scan backwards for module-level guards. Returns line index to insert before.
    Looks for __all__ = or if __name__ == patterns.
    Fallback: length of lines (EOF).
    """
    all_re = re.compile(r"^__all__\s*=")
    main_re = re.compile(r"^if\s+__name__")

    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if all_re.match(stripped) or main_re.match(stripped):
            return i
    return len(lines)


def _strategy_append_functions(
    baseline: str,
    branch_analyses: dict[str, FileAnalysis],
) -> str:
    """
    Collect all ADD_FUNCTION.content_after blocks from all branches.
    Deduplicate by function name.
    Insert before __all__ or if __name__ or at EOF.
    """
    lines = baseline.splitlines(keepends=True)

    # Collect existing function names
    existing_funcs = set(_PY_FUNC_RE.findall(baseline))

    seen_names: set[str] = set()
    blocks_to_append: list[str] = []

    for analysis in branch_analyses.values():
        for change in analysis.changes:
            if change.change_type == ChangeType.ADD_FUNCTION and change.content_after:
                func_name = change.target
                if func_name not in existing_funcs and func_name not in seen_names:
                    seen_names.add(func_name)
                    body = change.content_after
                    if not body.endswith("\n"):
                        body += "\n"
                    blocks_to_append.append(body)
            elif change.change_type in (
                ChangeType.ADD_CLASS,
                ChangeType.ADD_VARIABLE,
                ChangeType.ADD_CONSTANT,
                ChangeType.ADD_COMMENT,
            ) and change.content_after:
                # These also use APPEND_FUNCTIONS strategy
                if change.target not in seen_names:
                    seen_names.add(change.target)
                    body = change.content_after
                    if not body.endswith("\n"):
                        body += "\n"
                    blocks_to_append.append(body)

    if not blocks_to_append:
        return baseline

    plain_lines = [line.rstrip("\n").rstrip("\r") for line in lines]
    insert_pos = _find_insert_before_main(plain_lines)

    # Ensure two blank lines before new content
    separator = "\n\n"
    joined_blocks = separator.join(blocks_to_append)
    if not joined_blocks.startswith("\n"):
        joined_blocks = "\n\n" + joined_blocks

    before = "".join(lines[:insert_pos])
    after = "".join(lines[insert_pos:])

    # Trim trailing blank lines from `before` then add exactly two
    before = before.rstrip("\n") + "\n\n"

    return before + joined_blocks + ("\n" if not after.startswith("\n") else "") + after


def _find_class_body_end(lines: list[str], class_name: str) -> int:
    """
    Returns the line index where the class body ends (exclusive).
    Returns -1 if class_name not found.
    """
    class_pattern = re.compile(r"^(\s*)class\s+" + re.escape(class_name) + r"\s*[:(]")
    class_start = -1
    class_indent_len = 0

    for i, line in enumerate(lines):
        m = class_pattern.match(line)
        if m:
            class_start = i
            class_indent_len = len(m.group(1))
            break

    if class_start == -1:
        return -1

    end = len(lines)
    for i in range(class_start + 1, len(lines)):
        stripped = lines[i].rstrip()
        if stripped == "":
            continue
        # Count leading whitespace
        leading = len(lines[i]) - len(lines[i].lstrip())
        # If a non-blank line has indentation <= class indentation, class ended
        if leading <= class_indent_len:
            # But only if it's actually code (not a decorator for the class itself)
            if stripped.lstrip() and not stripped.lstrip().startswith("#"):
                end = i
                break

    return end


def _strategy_append_methods(
    baseline: str,
    branch_analyses: dict[str, FileAnalysis],
) -> str:
    """
    Group ADD_METHOD changes by class name.
    For each class, find the class body's closing boundary and insert methods before it.
    """
    lines = baseline.splitlines(keepends=True)
    plain_lines = [line.rstrip("\n").rstrip("\r") for line in lines]

    # Collect methods to add, grouped by class
    class_methods: dict[str, list[str]] = {}

    for analysis in branch_analyses.values():
        for change in analysis.changes:
            if change.change_type == ChangeType.ADD_METHOD and change.content_after:
                # target is "ClassName.method_name"
                if "." in change.target:
                    class_name = change.target.split(".", 1)[0]
                else:
                    class_name = change.target
                body = change.content_after
                if not body.endswith("\n"):
                    body += "\n"
                class_methods.setdefault(class_name, []).append(body)

    if not class_methods:
        return baseline

    # Process classes in reverse order of their position (so indices stay valid)
    class_positions: list[tuple[int, str]] = []
    class_pattern = re.compile(r"^(\s*)class\s+(\w+)\s*[:(]")
    for i, line in enumerate(plain_lines):
        m = class_pattern.match(line)
        if m:
            cls = m.group(2)
            if cls in class_methods:
                class_positions.append((i, cls))

    # Sort by position descending so insertions don't invalidate earlier indices
    class_positions.sort(key=lambda x: x[0], reverse=True)

    for _, cls in class_positions:
        end_idx = _find_class_body_end(plain_lines, cls)
        if end_idx == -1:
            continue

        methods_to_insert = class_methods[cls]
        insert_lines: list[str] = []
        for method_body in methods_to_insert:
            insert_lines.append("\n")
            for mline in method_body.splitlines(keepends=True):
                insert_lines.append(mline)

        lines = lines[:end_idx] + insert_lines + lines[end_idx:]
        # Update plain_lines for subsequent iterations
        plain_lines = [line.rstrip("\n").rstrip("\r") for line in lines]

    return "".join(lines)


_CHANGE_PRIORITY: dict[ChangeType, int] = {
    ChangeType.ADD_IMPORT: 0,
    ChangeType.ADD_DECORATOR: 1,
    ChangeType.ADD_CONSTANT: 2,
    ChangeType.ADD_VARIABLE: 2,
    ChangeType.ADD_FUNCTION: 3,
    ChangeType.ADD_CLASS: 3,
    ChangeType.ADD_METHOD: 4,
    ChangeType.MODIFY_FUNCTION: 5,
}
_DEFAULT_PRIORITY = 10


def _strategy_order_by_dependency(
    baseline: str,
    branch_analyses: dict[str, FileAnalysis],
) -> str:
    """
    Collect all additive changes across all branches.
    Sort by _CHANGE_PRIORITY.
    Apply in order: imports via COMBINE_IMPORTS logic, then functions via APPEND_FUNCTIONS,
    then methods via APPEND_METHODS.
    """
    # Separate changes by type bucket
    import_analyses: dict[str, FileAnalysis] = {}
    function_analyses: dict[str, FileAnalysis] = {}
    method_analyses: dict[str, FileAnalysis] = {}

    for branch, analysis in branch_analyses.items():
        import_changes: list[SemanticChange] = []
        function_changes: list[SemanticChange] = []
        method_changes: list[SemanticChange] = []

        for change in analysis.changes:
            priority = _CHANGE_PRIORITY.get(change.change_type, _DEFAULT_PRIORITY)
            if priority == 0:  # imports
                import_changes.append(change)
            elif change.change_type == ChangeType.ADD_METHOD:
                method_changes.append(change)
            else:
                function_changes.append(change)

        if import_changes:
            import_analyses[branch] = FileAnalysis(
                file_path=analysis.file_path,
                changes=import_changes,
                imports_added=analysis.imports_added,
                imports_removed=analysis.imports_removed,
            )
        if function_changes:
            function_analyses[branch] = FileAnalysis(
                file_path=analysis.file_path,
                changes=function_changes,
                functions_added=analysis.functions_added,
            )
        if method_changes:
            method_analyses[branch] = FileAnalysis(
                file_path=analysis.file_path,
                changes=method_changes,
            )

    result = baseline
    if import_analyses:
        result = _strategy_combine_imports(result, import_analyses)
    if function_analyses:
        result = _strategy_append_functions(result, function_analyses)
    if method_analyses:
        result = _strategy_append_methods(result, method_analyses)
    return result


def _deep_merge(base: object, override: object) -> object:
    """Deep merge two objects, with override taking priority."""
    if isinstance(base, dict) and isinstance(override, dict):
        result: dict[str, object] = dict(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = _deep_merge(result[k], v)
            else:
                result[k] = v
        return result
    return override


def _strategy_combine_configs(
    file_path: str,
    baseline: str,
    branch_analyses: dict[str, FileAnalysis],
) -> str:
    """
    For JSON: parse baseline + each branch's ADD_CONFIG_KEY changes.
    Deep-merge dicts. Re-serialize with json.dumps(indent=2).

    For TOML: use tomllib to parse baseline, apply ADD_CONFIG_KEY entries.
    Re-serialize via line-level insertion if tomli_w not available.

    For YAML: fall back to AI_REQUIRED (return baseline unchanged).
    """
    ext = Path(file_path).suffix.lower()

    if ext == ".json":
        try:
            base_data: object = json.loads(baseline) if baseline.strip() else {}
        except json.JSONDecodeError:
            return baseline

        result_data = base_data
        for analysis in branch_analyses.values():
            for change in analysis.changes:
                if change.change_type in (ChangeType.ADD_CONFIG_KEY, ChangeType.MODIFY_CONFIG_KEY):
                    if change.content_after:
                        try:
                            branch_data = json.loads(change.content_after)
                            result_data = _deep_merge(result_data, branch_data)
                        except json.JSONDecodeError:
                            pass

        return json.dumps(result_data, indent=2, ensure_ascii=False) + "\n"

    if ext == ".toml":
        try:
            base_toml: dict[str, object] = tomllib.loads(baseline) if baseline.strip() else {}
        except Exception:
            return baseline

        result_toml: dict[str, object] = dict(base_toml)
        for analysis in branch_analyses.values():
            for change in analysis.changes:
                if change.change_type in (ChangeType.ADD_CONFIG_KEY, ChangeType.MODIFY_CONFIG_KEY):
                    if change.content_after:
                        try:
                            branch_toml = tomllib.loads(change.content_after)
                            merged = _deep_merge(result_toml, branch_toml)
                            if isinstance(merged, dict):
                                result_toml = merged
                        except Exception:
                            pass

        # Try tomli_w for proper serialization
        try:
            import tomli_w  # type: ignore[import]
            return tomli_w.dumps(result_toml)
        except ImportError:
            pass

        # Fallback: return baseline (cannot re-serialize without tomli_w)
        return baseline

    # YAML or other: return baseline unchanged (caller should treat as AI_REQUIRED)
    return baseline


# ---------------------------------------------------------------------------
# AI Fallback Merger
# ---------------------------------------------------------------------------


async def _run_ai_merge_query(
    system: str,
    user: str,
    model: str,
    env: dict[str, str],
) -> str:
    """
    Run a single SDK query. Return the assistant's text content.
    Raises ClaudeSDKError on failure.
    """
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

    options = ClaudeAgentOptions(
        system_prompt=system,
        max_turns=5,
        model=model,
    )
    result_text = ""
    async for message in query(prompt=user, options=options, env=env):
        if isinstance(message, ResultMessage) and message.result:
            result_text = message.result
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "text"):
                    result_text += block.text
    return result_text


async def _ai_merge_file(
    file_path: str,
    baseline: str,
    branch_contents: dict[str, str],
    conflicts: list[ConflictRegion],
    config: GolemConfig,
    repo_root: Path,
) -> FileMergeResult:
    """
    Build prompts and run a single SDK query to merge file content.
    Returns AI_MERGED on success, NEEDS_HUMAN_REVIEW on exception or empty response.
    """
    from golem.config import sdk_env

    system = (
        "You are a code merge expert for a Python project.\n"
        "Your task is to produce a single merged file that incorporates all changes.\n"
        "Return ONLY the merged file content, no explanation, no markdown fences."
    )

    parts = [f"Merge the following versions of {file_path}:\n\nBASELINE:\n{baseline}\n"]
    for branch_name, content in branch_contents.items():
        parts.append(f"\nBRANCH {branch_name} VERSION:\n{content}\n")

    if conflicts:
        parts.append("\nCONFLICTS TO RESOLVE:")
        for c in conflicts:
            parts.append(f"- {c.location}: {c.reason} (severity: {c.severity.value})")

    parts.append("\nReturn the complete merged file content:")
    user_prompt = "\n".join(parts)

    try:
        merged = await _run_ai_merge_query(
            system=system,
            user=user_prompt,
            model=config.validator_model,
            env=sdk_env(),
        )
        if not merged.strip():
            return FileMergeResult(
                decision=MergeDecision.NEEDS_HUMAN_REVIEW,
                file_path=file_path,
                merged_content=None,
                conflicts_remaining=conflicts,
                explanation="AI returned empty response",
            )
        return FileMergeResult(
            decision=MergeDecision.AI_MERGED,
            file_path=file_path,
            merged_content=merged,
            conflicts_resolved=conflicts,
            ai_calls_made=1,
            explanation="Merged by AI fallback",
        )
    except Exception as exc:
        return FileMergeResult(
            decision=MergeDecision.NEEDS_HUMAN_REVIEW,
            file_path=file_path,
            merged_content=None,
            conflicts_remaining=conflicts,
            explanation=f"AI merge failed: {exc}",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _get_file_at_ref(repo_root: Path, ref: str, file_path: str) -> str:
    """git show <ref>:<file_path> — returns "" if file does not exist at ref."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{file_path}"],
        cwd=repo_root, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def _get_changed_files(repo_root: Path, base_ref: str, head_ref: str) -> list[str]:
    """git diff --name-only <base_ref>...<head_ref> — relative paths."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...{head_ref}"],
        cwd=repo_root, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Merge Report Persistence
# ---------------------------------------------------------------------------


def save_merge_report(report: MergeReport, golem_dir: Path, run_id: str) -> Path:
    """
    Write report to golem_dir/merge_reports/<run_id>.json.
    Returns the path written.
    """
    reports_dir = golem_dir / "merge_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    file_results_serialized: dict[str, object] = {}
    for file_path, result in report.file_results.items():
        entry: dict[str, object] = {
            "decision": result.decision.value,
            "conflicts_resolved": len(result.conflicts_resolved),
            "conflicts_remaining": len(result.conflicts_remaining),
            "ai_calls_made": result.ai_calls_made,
            "explanation": result.explanation,
        }
        if result.error:
            entry["error"] = result.error
        if result.conflicts_resolved:
            entry["strategy"] = result.conflicts_resolved[0].merge_strategy.value if result.conflicts_resolved[0].merge_strategy else ""
        file_results_serialized[file_path] = entry

    data: dict[str, object] = {
        "success": report.success,
        "branches_merged": report.branches_merged,
        "stats": {
            "files_processed": report.stats.files_processed,
            "files_auto_merged": report.stats.files_auto_merged,
            "files_ai_merged": report.stats.files_ai_merged,
            "files_need_review": report.stats.files_need_review,
            "files_failed": report.stats.files_failed,
            "conflicts_detected": report.stats.conflicts_detected,
            "conflicts_auto_resolved": report.stats.conflicts_auto_resolved,
            "conflicts_ai_resolved": report.stats.conflicts_ai_resolved,
            "ai_calls_made": report.stats.ai_calls_made,
            "duration_ms": report.stats.duration_ms,
        },
        "file_results": file_results_serialized,
    }
    if report.error:
        data["error"] = report.error

    out_path = reports_dir / f"{run_id}.json"
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# MergeResolver — The Orchestrator
# ---------------------------------------------------------------------------


class MergeResolver:
    """
    Orchestrates the full pre-merge resolution pipeline for a set of branches
    targeting a common base.
    """

    def __init__(
        self,
        repo_root: Path,
        config: GolemConfig,
        enable_ai: bool = True,
    ) -> None:
        self._repo_root = repo_root
        self._config = config
        self._enable_ai = enable_ai
        self._detector = ConflictDetector()

    def pre_resolve(
        self,
        branches: list[str],
        target_branch: str,
    ) -> MergeReport:
        """
        Synchronous entry point called from merge_group_branches() before git merge.

        For each file touched by any branch vs target_branch:
          1. Get baseline content from target_branch
          2. Get each branch's version
          3. Run SemanticAnalyzer on (baseline, branch_content) per branch
          4. Run ConflictDetector on all FileAnalysis objects
          5. If no conflicts -> DIRECT_COPY
          6. If all conflicts canAutoMerge -> apply deterministic strategy
          7. If any AI_REQUIRED conflicts AND enable_ai -> schedule for async AI pass
          8. Otherwise -> NEEDS_HUMAN_REVIEW

        Returns MergeReport summarizing what was pre-resolved.
        """
        start_ms = int(time.time() * 1000)

        if self._enable_ai:
            # Run async implementation via asyncio.run()
            try:
                report = asyncio.run(self._pre_resolve_async(branches, target_branch))
            except RuntimeError:
                # Already inside event loop (e.g. tests) — fall back to sync-only
                report = self._pre_resolve_sync(branches, target_branch)
        else:
            report = self._pre_resolve_sync(branches, target_branch)

        report.stats.duration_ms = int(time.time() * 1000) - start_ms
        return report

    def _collect_file_analyses(
        self,
        branches: list[str],
        target_branch: str,
    ) -> dict[str, dict[str, FileAnalysis]]:
        """
        Returns {file_path: {branch_name: FileAnalysis}} for all files
        changed by any branch relative to target_branch.
        """
        all_files: set[str] = set()
        for branch in branches:
            changed = _get_changed_files(self._repo_root, target_branch, branch)
            all_files.update(changed)

        file_branch_analyses: dict[str, dict[str, FileAnalysis]] = {}

        for file_path in all_files:
            baseline = _get_file_at_ref(self._repo_root, target_branch, file_path)
            branch_analyses: dict[str, FileAnalysis] = {}

            for branch in branches:
                branch_content = _get_file_at_ref(self._repo_root, branch, file_path)
                if branch_content != baseline:
                    branch_analyses[branch] = analyze_diff(file_path, baseline, branch_content)

            if branch_analyses:
                file_branch_analyses[file_path] = branch_analyses

        return file_branch_analyses

    def _apply_strategy(
        self,
        strategy: MergeStrategy,
        file_path: str,
        baseline: str,
        branch_analyses: dict[str, FileAnalysis],
    ) -> str:
        """Dispatch to the correct strategy function."""
        match strategy:
            case MergeStrategy.COMBINE_IMPORTS:
                return _strategy_combine_imports(baseline, branch_analyses)
            case MergeStrategy.APPEND_FUNCTIONS:
                return _strategy_append_functions(baseline, branch_analyses)
            case MergeStrategy.APPEND_METHODS:
                return _strategy_append_methods(baseline, branch_analyses)
            case MergeStrategy.ORDER_BY_DEPENDENCY:
                return _strategy_order_by_dependency(baseline, branch_analyses)
            case MergeStrategy.COMBINE_CONFIGS:
                return _strategy_combine_configs(file_path, baseline, branch_analyses)
            case _:
                raise ValueError(f"No handler for strategy: {strategy}")

    def _pre_resolve_sync(
        self,
        branches: list[str],
        target_branch: str,
    ) -> MergeReport:
        """Synchronous-only implementation (no AI fallback)."""
        stats = MergeStats()
        file_results: dict[str, FileMergeResult] = {}

        file_branch_analyses = self._collect_file_analyses(branches, target_branch)
        stats.files_processed = len(file_branch_analyses)

        for file_path, branch_analyses in file_branch_analyses.items():
            baseline = _get_file_at_ref(self._repo_root, target_branch, file_path)

            if len(branch_analyses) == 1:
                # Only one branch changed this file — direct copy
                branch = next(iter(branch_analyses))
                branch_content = _get_file_at_ref(self._repo_root, branch, file_path)
                file_results[file_path] = FileMergeResult(
                    decision=MergeDecision.DIRECT_COPY,
                    file_path=file_path,
                    merged_content=branch_content,
                    explanation="Single branch change — direct copy",
                )
                stats.files_auto_merged += 1
                continue

            conflicts = self._detector.detect_conflicts(branch_analyses)
            stats.conflicts_detected += len(conflicts)

            if not conflicts:
                # No overlapping changes — take the last branch's version
                last_branch = list(branch_analyses.keys())[-1]
                branch_content = _get_file_at_ref(self._repo_root, last_branch, file_path)
                file_results[file_path] = FileMergeResult(
                    decision=MergeDecision.DIRECT_COPY,
                    file_path=file_path,
                    merged_content=branch_content,
                    explanation="No conflicts detected",
                )
                stats.files_auto_merged += 1
                continue

            auto_conflicts = [c for c in conflicts if c.can_auto_merge]
            hard_conflicts = [c for c in conflicts if not c.can_auto_merge]

            if not hard_conflicts:
                # All conflicts are auto-mergeable
                strategy = auto_conflicts[0].merge_strategy
                if strategy is None:
                    strategy = MergeStrategy.APPEND_FUNCTIONS

                try:
                    merged = self._apply_strategy(strategy, file_path, baseline, branch_analyses)
                    file_results[file_path] = FileMergeResult(
                        decision=MergeDecision.AUTO_MERGED,
                        file_path=file_path,
                        merged_content=merged,
                        conflicts_resolved=auto_conflicts,
                        explanation=f"Auto-merged with strategy: {strategy.value}",
                    )
                    stats.files_auto_merged += 1
                    stats.conflicts_auto_resolved += len(auto_conflicts)
                except Exception as exc:
                    file_results[file_path] = FileMergeResult(
                        decision=MergeDecision.FAILED,
                        file_path=file_path,
                        merged_content=None,
                        conflicts_remaining=auto_conflicts,
                        error=str(exc),
                    )
                    stats.files_failed += 1
            else:
                # Has hard conflicts — needs review (no AI in sync mode)
                file_results[file_path] = FileMergeResult(
                    decision=MergeDecision.NEEDS_HUMAN_REVIEW,
                    file_path=file_path,
                    merged_content=None,
                    conflicts_remaining=hard_conflicts,
                    explanation="; ".join(c.reason for c in hard_conflicts),
                )
                stats.files_need_review += 1
                print(
                    f"[MERGE] NEEDS_HUMAN_REVIEW: {file_path} — {hard_conflicts[0].reason}",
                    file=sys.stderr,
                )

        success = stats.files_failed == 0 and stats.files_need_review == 0
        return MergeReport(
            success=success,
            branches_merged=branches,
            file_results=file_results,
            stats=stats,
        )

    async def _pre_resolve_async(
        self,
        branches: list[str],
        target_branch: str,
    ) -> MergeReport:
        """Async implementation — called from pre_resolve() via asyncio.run()."""
        stats = MergeStats()
        file_results: dict[str, FileMergeResult] = {}

        file_branch_analyses = self._collect_file_analyses(branches, target_branch)
        stats.files_processed = len(file_branch_analyses)

        # First pass: synchronous resolution
        ai_queue: list[tuple[str, dict[str, FileAnalysis], list[ConflictRegion]]] = []

        for file_path, branch_analyses in file_branch_analyses.items():
            baseline = _get_file_at_ref(self._repo_root, target_branch, file_path)

            if len(branch_analyses) == 1:
                branch = next(iter(branch_analyses))
                branch_content = _get_file_at_ref(self._repo_root, branch, file_path)
                file_results[file_path] = FileMergeResult(
                    decision=MergeDecision.DIRECT_COPY,
                    file_path=file_path,
                    merged_content=branch_content,
                    explanation="Single branch change — direct copy",
                )
                stats.files_auto_merged += 1
                continue

            conflicts = self._detector.detect_conflicts(branch_analyses)
            stats.conflicts_detected += len(conflicts)

            if not conflicts:
                last_branch = list(branch_analyses.keys())[-1]
                branch_content = _get_file_at_ref(self._repo_root, last_branch, file_path)
                file_results[file_path] = FileMergeResult(
                    decision=MergeDecision.DIRECT_COPY,
                    file_path=file_path,
                    merged_content=branch_content,
                    explanation="No conflicts detected",
                )
                stats.files_auto_merged += 1
                continue

            auto_conflicts = [c for c in conflicts if c.can_auto_merge]
            hard_conflicts = [c for c in conflicts if not c.can_auto_merge]

            if not hard_conflicts:
                strategy = auto_conflicts[0].merge_strategy
                if strategy is None:
                    strategy = MergeStrategy.APPEND_FUNCTIONS
                try:
                    merged = self._apply_strategy(strategy, file_path, baseline, branch_analyses)
                    file_results[file_path] = FileMergeResult(
                        decision=MergeDecision.AUTO_MERGED,
                        file_path=file_path,
                        merged_content=merged,
                        conflicts_resolved=auto_conflicts,
                        explanation=f"Auto-merged with strategy: {strategy.value}",
                    )
                    stats.files_auto_merged += 1
                    stats.conflicts_auto_resolved += len(auto_conflicts)
                except Exception as exc:
                    file_results[file_path] = FileMergeResult(
                        decision=MergeDecision.FAILED,
                        file_path=file_path,
                        merged_content=None,
                        conflicts_remaining=auto_conflicts,
                        error=str(exc),
                    )
                    stats.files_failed += 1
            elif self._enable_ai:
                # Schedule for AI resolution
                ai_queue.append((file_path, branch_analyses, hard_conflicts))
            else:
                file_results[file_path] = FileMergeResult(
                    decision=MergeDecision.NEEDS_HUMAN_REVIEW,
                    file_path=file_path,
                    merged_content=None,
                    conflicts_remaining=hard_conflicts,
                    explanation="; ".join(c.reason for c in hard_conflicts),
                )
                stats.files_need_review += 1
                print(
                    f"[MERGE] NEEDS_HUMAN_REVIEW: {file_path} — {hard_conflicts[0].reason}",
                    file=sys.stderr,
                )

        # Second pass: AI resolution for queued files
        for file_path, branch_analyses, hard_conflicts in ai_queue:
            baseline = _get_file_at_ref(self._repo_root, target_branch, file_path)
            branch_contents = {
                branch: _get_file_at_ref(self._repo_root, branch, file_path)
                for branch in branch_analyses
            }
            result = await _ai_merge_file(
                file_path=file_path,
                baseline=baseline,
                branch_contents=branch_contents,
                conflicts=hard_conflicts,
                config=self._config,
                repo_root=self._repo_root,
            )
            file_results[file_path] = result
            stats.ai_calls_made += result.ai_calls_made
            if result.decision == MergeDecision.AI_MERGED:
                stats.files_ai_merged += 1
                stats.conflicts_ai_resolved += len(result.conflicts_resolved)
            elif result.decision == MergeDecision.NEEDS_HUMAN_REVIEW:
                stats.files_need_review += 1
                print(
                    f"[MERGE] AI failed, NEEDS_HUMAN_REVIEW: {file_path}",
                    file=sys.stderr,
                )
            else:
                stats.files_failed += 1

        success = stats.files_failed == 0 and stats.files_need_review == 0
        return MergeReport(
            success=success,
            branches_merged=branches,
            file_results=file_results,
            stats=stats,
        )
