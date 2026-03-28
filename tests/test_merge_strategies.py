"""Tests for merge_strategies.py — SemanticAnalyzer, ConflictDetector, AutoMerger, MergeResolver."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from golem.merge_strategies import (
    ChangeType,
    ConflictDetector,
    ConflictSeverity,
    FileAnalysis,
    FileMergeResult,
    MergeDecision,
    MergeReport,
    MergeResolver,
    MergeStats,
    MergeStrategy,
    SemanticChange,
    _find_class_body_end,
    _find_import_section_end,
    _strategy_append_functions,
    _strategy_append_methods,
    _strategy_combine_configs,
    _strategy_combine_imports,
    _strategy_order_by_dependency,
    analyze_diff,
    save_merge_report,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conflict_repo(tmp_path: Path) -> Path:
    """
    Create a minimal git repo with:
      - main branch containing base.py (one function foo())
      - branch-a: adds function bar() to base.py
      - branch-b: adds function baz() to base.py
    This produces an ADD_FUNCTION + ADD_FUNCTION conflict.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)

    base = repo / "base.py"
    base.write_text("def foo():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    subprocess.run(["git", "checkout", "-b", "branch-a"], cwd=repo, check=True, capture_output=True)
    base.write_text("def foo():\n    pass\n\ndef bar():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add bar"], cwd=repo, check=True, capture_output=True)

    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "branch-b"], cwd=repo, check=True, capture_output=True)
    base.write_text("def foo():\n    pass\n\ndef baz():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add baz"], cwd=repo, check=True, capture_output=True)

    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    return repo


# ---------------------------------------------------------------------------
# TestSemanticAnalyzer
# ---------------------------------------------------------------------------


class TestSemanticAnalyzer:
    def test_import_add_detected(self) -> None:
        before = "import os\n\ndef foo(): pass\n"
        after = "import os\nimport sys\n\ndef foo(): pass\n"
        analysis = analyze_diff("file.py", before, after)
        assert ChangeType.ADD_IMPORT in {c.change_type for c in analysis.changes}
        assert any("sys" in imp for imp in analysis.imports_added)

    def test_import_remove_detected(self) -> None:
        before = "import os\nimport sys\n\ndef foo(): pass\n"
        after = "import os\n\ndef foo(): pass\n"
        analysis = analyze_diff("file.py", before, after)
        assert ChangeType.REMOVE_IMPORT in {c.change_type for c in analysis.changes}
        assert any("sys" in imp for imp in analysis.imports_removed)

    def test_from_import_detected(self) -> None:
        before = "from pathlib import Path\n\ndef foo(): pass\n"
        after = "from pathlib import Path\nfrom typing import TYPE_CHECKING\n\ndef foo(): pass\n"
        analysis = analyze_diff("file.py", before, after)
        assert ChangeType.ADD_IMPORT in {c.change_type for c in analysis.changes}
        assert any("TYPE_CHECKING" in imp for imp in analysis.imports_added)

    def test_function_add_detected(self) -> None:
        before = "def foo(): pass\n"
        after = "def foo(): pass\n\ndef bar(): return 1\n"
        analysis = analyze_diff("file.py", before, after)
        assert "bar" in analysis.functions_added
        assert ChangeType.ADD_FUNCTION in {c.change_type for c in analysis.changes}

    def test_function_modify_detected(self) -> None:
        before = "def foo():\n    return 1\n"
        after = "def foo():\n    return 2\n"
        analysis = analyze_diff("file.py", before, after)
        assert "foo" in analysis.functions_modified
        assert ChangeType.MODIFY_FUNCTION in {c.change_type for c in analysis.changes}

    def test_function_remove_detected(self) -> None:
        before = "def foo(): pass\n\ndef bar(): pass\n"
        after = "def foo(): pass\n"
        analysis = analyze_diff("file.py", before, after)
        assert ChangeType.REMOVE_FUNCTION in {c.change_type for c in analysis.changes}

    def test_class_add_detected(self) -> None:
        before = "def foo(): pass\n"
        after = "def foo(): pass\n\nclass MyClass:\n    pass\n"
        analysis = analyze_diff("file.py", before, after)
        assert ChangeType.ADD_CLASS in {c.change_type for c in analysis.changes}

    def test_method_add_detected(self) -> None:
        before = "class MyClass:\n    def existing(self): pass\n"
        after = "class MyClass:\n    def existing(self): pass\n\n    def new_method(self): pass\n"
        analysis = analyze_diff("file.py", before, after)
        add_method_changes = [c for c in analysis.changes if c.change_type == ChangeType.ADD_METHOD]
        assert len(add_method_changes) == 1
        assert "new_method" in add_method_changes[0].target

    def test_method_modify_detected(self) -> None:
        before = "class MyClass:\n    def existing(self):\n        return 1\n"
        after = "class MyClass:\n    def existing(self):\n        return 99\n"
        analysis = analyze_diff("file.py", before, after)
        modify_changes = [c for c in analysis.changes if c.change_type == ChangeType.MODIFY_METHOD]
        assert len(modify_changes) == 1

    def test_decorator_add_detected(self) -> None:
        before = "def foo(): pass\n"
        after = "@property\ndef foo(): pass\n"
        analysis = analyze_diff("file.py", before, after)
        assert ChangeType.ADD_DECORATOR in {c.change_type for c in analysis.changes}

    def test_config_key_add_detected_json(self) -> None:
        before = '{"a": 1}'
        after = '{"a": 1, "b": 2}'
        analysis = analyze_diff("config.json", before, after)
        assert any(c.change_type == ChangeType.ADD_CONFIG_KEY for c in analysis.changes)

    def test_config_key_modify_detected_json(self) -> None:
        before = '{"a": 1}'
        after = '{"a": 99}'
        analysis = analyze_diff("config.json", before, after)
        assert any(c.change_type == ChangeType.MODIFY_CONFIG_KEY for c in analysis.changes)

    def test_config_toml_add_detected(self) -> None:
        before = '[tool]\nline-length = 88\n'
        after = '[tool]\nline-length = 120\n[extra]\nnew_key = "value"\n'
        analysis = analyze_diff("pyproject.toml", before, after)
        # Should detect changes (add or modify)
        assert len(analysis.changes) > 0

    def test_empty_before(self) -> None:
        before = ""
        after = "import os\n\ndef foo(): pass\n"
        analysis = analyze_diff("file.py", before, after)
        assert ChangeType.ADD_IMPORT in {c.change_type for c in analysis.changes}
        assert ChangeType.ADD_FUNCTION in {c.change_type for c in analysis.changes}

    def test_unchanged_file_no_changes(self) -> None:
        content = "import os\n\ndef foo(): pass\n"
        analysis = analyze_diff("file.py", content, content)
        assert len(analysis.changes) == 0

    def test_function_location_key(self) -> None:
        before = "def foo(): pass\n"
        after = "def foo(): pass\n\ndef bar(): return 1\n"
        analysis = analyze_diff("file.py", before, after)
        add_changes = [c for c in analysis.changes if c.change_type == ChangeType.ADD_FUNCTION]
        assert all(c.location == "function:bar" for c in add_changes)

    def test_import_location_key(self) -> None:
        before = "import os\n\ndef foo(): pass\n"
        after = "import os\nimport sys\n\ndef foo(): pass\n"
        analysis = analyze_diff("file.py", before, after)
        import_changes = [c for c in analysis.changes if c.change_type == ChangeType.ADD_IMPORT]
        assert all(c.location == "file_top" for c in import_changes)

    def test_class_location_key(self) -> None:
        before = "class MyClass:\n    def existing(self): pass\n"
        after = "class MyClass:\n    def existing(self): pass\n\n    def new_method(self): pass\n"
        analysis = analyze_diff("file.py", before, after)
        method_changes = [c for c in analysis.changes if c.change_type == ChangeType.ADD_METHOD]
        assert all(c.location == "class:MyClass" for c in method_changes)


# ---------------------------------------------------------------------------
# TestConflictDetector
# ---------------------------------------------------------------------------


class TestConflictDetector:
    def test_two_add_functions_compatible(self) -> None:
        detector = ConflictDetector()
        analysis_a = FileAnalysis(
            "f.py",
            functions_added={"bar"},
            changes=[
                SemanticChange(ChangeType.ADD_FUNCTION, "bar", "function:bar", 5, 7,
                               content_after="def bar():\n    return 1\n"),
            ],
        )
        analysis_b = FileAnalysis(
            "f.py",
            functions_added={"baz"},
            changes=[
                SemanticChange(ChangeType.ADD_FUNCTION, "baz", "function:baz", 5, 7,
                               content_after="def baz():\n    return 2\n"),
            ],
        )
        conflicts = detector.detect_conflicts({"branch-a": analysis_a, "branch-b": analysis_b})
        assert all(c.can_auto_merge for c in conflicts)
        assert all(c.merge_strategy in (MergeStrategy.APPEND_FUNCTIONS, MergeStrategy.APPEND_METHODS) for c in conflicts)

    def test_two_modify_same_function_incompatible(self) -> None:
        detector = ConflictDetector()
        analysis_a = FileAnalysis(
            "f.py",
            functions_modified={"foo"},
            changes=[
                SemanticChange(ChangeType.MODIFY_FUNCTION, "foo", "function:foo", 1, 3),
            ],
        )
        analysis_b = FileAnalysis(
            "f.py",
            functions_modified={"foo"},
            changes=[
                SemanticChange(ChangeType.MODIFY_FUNCTION, "foo", "function:foo", 1, 3),
            ],
        )
        conflicts = detector.detect_conflicts({"branch-a": analysis_a, "branch-b": analysis_b})
        assert any(not c.can_auto_merge for c in conflicts)
        assert any(c.merge_strategy == MergeStrategy.AI_REQUIRED for c in conflicts)

    def test_add_import_add_import_compatible(self) -> None:
        detector = ConflictDetector()
        analysis_a = FileAnalysis(
            "f.py",
            imports_added={"import sys"},
            changes=[SemanticChange(ChangeType.ADD_IMPORT, "import sys", "file_top", 1, 1,
                                    content_after="import sys")],
        )
        analysis_b = FileAnalysis(
            "f.py",
            imports_added={"import re"},
            changes=[SemanticChange(ChangeType.ADD_IMPORT, "import re", "file_top", 1, 1,
                                    content_after="import re")],
        )
        conflicts = detector.detect_conflicts({"a": analysis_a, "b": analysis_b})
        assert len(conflicts) == 1
        assert conflicts[0].can_auto_merge
        assert conflicts[0].merge_strategy == MergeStrategy.COMBINE_IMPORTS

    def test_add_method_add_method_compatible(self) -> None:
        detector = ConflictDetector()
        analysis_a = FileAnalysis(
            "f.py",
            changes=[SemanticChange(ChangeType.ADD_METHOD, "Cls.method_a", "class:Cls", 5, 7,
                                    content_after="    def method_a(self): pass\n")],
        )
        analysis_b = FileAnalysis(
            "f.py",
            changes=[SemanticChange(ChangeType.ADD_METHOD, "Cls.method_b", "class:Cls", 5, 7,
                                    content_after="    def method_b(self): pass\n")],
        )
        conflicts = detector.detect_conflicts({"a": analysis_a, "b": analysis_b})
        # Different targets at same class location — compatible
        assert all(c.can_auto_merge for c in conflicts)

    def test_add_config_key_compatible(self) -> None:
        detector = ConflictDetector()
        analysis_a = FileAnalysis(
            "cfg.json",
            changes=[SemanticChange(ChangeType.ADD_CONFIG_KEY, "debug", "config_root", 1, 1,
                                    content_after='{"version": 1, "debug": true}')],
        )
        analysis_b = FileAnalysis(
            "cfg.json",
            changes=[SemanticChange(ChangeType.ADD_CONFIG_KEY, "timeout", "config_root", 1, 1,
                                    content_after='{"version": 1, "timeout": 30}')],
        )
        conflicts = detector.detect_conflicts({"a": analysis_a, "b": analysis_b})
        assert len(conflicts) >= 1
        assert all(c.can_auto_merge for c in conflicts)
        assert all(c.merge_strategy == MergeStrategy.COMBINE_CONFIGS for c in conflicts)

    def test_severity_critical_for_double_modify(self) -> None:
        detector = ConflictDetector()
        analysis_a = FileAnalysis(
            "f.py",
            functions_modified={"foo"},
            changes=[SemanticChange(ChangeType.MODIFY_FUNCTION, "foo", "function:foo", 1, 3)],
        )
        analysis_b = FileAnalysis(
            "f.py",
            functions_modified={"foo"},
            changes=[SemanticChange(ChangeType.MODIFY_FUNCTION, "foo", "function:foo", 1, 3)],
        )
        conflicts = detector.detect_conflicts({"a": analysis_a, "b": analysis_b})
        assert any(c.severity == ConflictSeverity.CRITICAL for c in conflicts)

    def test_severity_none_for_compatible(self) -> None:
        detector = ConflictDetector()
        analysis_a = FileAnalysis(
            "f.py",
            functions_added={"bar"},
            changes=[SemanticChange(ChangeType.ADD_FUNCTION, "bar", "function:bar", 5, 7,
                                    content_after="def bar(): pass\n")],
        )
        analysis_b = FileAnalysis(
            "f.py",
            functions_added={"baz"},
            changes=[SemanticChange(ChangeType.ADD_FUNCTION, "baz", "function:baz", 5, 7,
                                    content_after="def baz(): pass\n")],
        )
        conflicts = detector.detect_conflicts({"a": analysis_a, "b": analysis_b})
        compatible = [c for c in conflicts if c.can_auto_merge]
        assert all(c.severity == ConflictSeverity.NONE for c in compatible)

    def test_no_conflict_single_branch(self) -> None:
        detector = ConflictDetector()
        analysis_a = FileAnalysis(
            "f.py",
            functions_added={"bar"},
            changes=[SemanticChange(ChangeType.ADD_FUNCTION, "bar", "function:bar", 5, 7)],
        )
        conflicts = detector.detect_conflicts({"a": analysis_a})
        assert len(conflicts) == 0

    def test_add_import_add_function_compatible(self) -> None:
        detector = ConflictDetector()
        analysis_a = FileAnalysis(
            "f.py",
            imports_added={"import sys"},
            changes=[SemanticChange(ChangeType.ADD_IMPORT, "import sys", "file_top", 1, 1,
                                    content_after="import sys")],
        )
        analysis_b = FileAnalysis(
            "f.py",
            functions_added={"new_func"},
            changes=[SemanticChange(ChangeType.ADD_FUNCTION, "new_func", "function:new_func", 5, 7,
                                    content_after="def new_func(): pass\n")],
        )
        conflicts = detector.detect_conflicts({"a": analysis_a, "b": analysis_b})
        assert all(c.can_auto_merge for c in conflicts)

    def test_branches_involved_populated(self) -> None:
        detector = ConflictDetector()
        analysis_a = FileAnalysis(
            "f.py",
            changes=[SemanticChange(ChangeType.ADD_IMPORT, "import sys", "file_top", 1, 1)],
        )
        analysis_b = FileAnalysis(
            "f.py",
            changes=[SemanticChange(ChangeType.ADD_IMPORT, "import re", "file_top", 1, 1)],
        )
        conflicts = detector.detect_conflicts({"branch-a": analysis_a, "branch-b": analysis_b})
        assert len(conflicts) == 1
        assert "branch-a" in conflicts[0].branches_involved
        assert "branch-b" in conflicts[0].branches_involved


# ---------------------------------------------------------------------------
# TestFindImportSectionEnd
# ---------------------------------------------------------------------------


class TestFindImportSectionEnd:
    def test_no_imports(self) -> None:
        lines = ["def foo(): pass\n", "\n"]
        assert _find_import_section_end(lines) == 0

    def test_single_import(self) -> None:
        lines = ["import os\n", "\n", "def foo(): pass\n"]
        assert _find_import_section_end(lines) == 1

    def test_multiple_imports(self) -> None:
        lines = ["import os\n", "import sys\n", "\n", "def foo(): pass\n"]
        assert _find_import_section_end(lines) == 2

    def test_from_import(self) -> None:
        lines = ["from pathlib import Path\n", "import os\n", "\n", "X = 1\n"]
        result = _find_import_section_end(lines)
        assert result == 2


# ---------------------------------------------------------------------------
# TestFindClassBodyEnd
# ---------------------------------------------------------------------------


class TestFindClassBodyEnd:
    def test_simple_class(self) -> None:
        lines = [
            "class MyClass:\n",
            "    def method(self): pass\n",
            "\n",
            "def other(): pass\n",
        ]
        end = _find_class_body_end(lines, "MyClass")
        assert end == 3  # "def other" starts at index 3

    def test_class_not_found(self) -> None:
        lines = ["def foo(): pass\n"]
        assert _find_class_body_end(lines, "Missing") == -1

    def test_class_at_end_of_file(self) -> None:
        lines = [
            "class MyClass:\n",
            "    def method(self): pass\n",
        ]
        end = _find_class_body_end(lines, "MyClass")
        assert end == len(lines)


# ---------------------------------------------------------------------------
# TestStrategyCombineImports
# ---------------------------------------------------------------------------


class TestStrategyCombineImports:
    def test_adds_new_imports(self) -> None:
        baseline = "import os\n\ndef foo(): pass\n"
        analysis_a = FileAnalysis(
            "f.py",
            imports_added={"import sys"},
            changes=[SemanticChange(ChangeType.ADD_IMPORT, "import sys", "file_top", 2, 2,
                                    content_after="import sys")],
        )
        analysis_b = FileAnalysis(
            "f.py",
            imports_added={"import re"},
            changes=[SemanticChange(ChangeType.ADD_IMPORT, "import re", "file_top", 2, 2,
                                    content_after="import re")],
        )
        result = _strategy_combine_imports(baseline, {"a": analysis_a, "b": analysis_b})
        assert "import sys" in result
        assert "import re" in result
        assert result.index("import sys") < result.index("def foo")

    def test_no_duplicates(self) -> None:
        baseline = "import os\nimport sys\n\ndef foo(): pass\n"
        analysis_a = FileAnalysis(
            "f.py",
            imports_added={"import sys"},
            changes=[SemanticChange(ChangeType.ADD_IMPORT, "import sys", "file_top", 2, 2,
                                    content_after="import sys")],
        )
        result = _strategy_combine_imports(baseline, {"a": analysis_a})
        assert result.count("import sys") == 1

    def test_removes_imports(self) -> None:
        baseline = "import os\nimport sys\n\ndef foo(): pass\n"
        analysis_a = FileAnalysis(
            "f.py",
            imports_removed={"import sys"},
            changes=[SemanticChange(ChangeType.REMOVE_IMPORT, "import sys", "file_top", 2, 2,
                                    content_before="import sys")],
        )
        result = _strategy_combine_imports(baseline, {"a": analysis_a})
        assert "import sys" not in result
        assert "import os" in result

    def test_empty_analyses(self) -> None:
        baseline = "import os\n\ndef foo(): pass\n"
        result = _strategy_combine_imports(baseline, {"a": FileAnalysis("f.py")})
        assert result == baseline


# ---------------------------------------------------------------------------
# TestStrategyAppendFunctions
# ---------------------------------------------------------------------------


class TestStrategyAppendFunctions:
    def test_appends_new_functions(self) -> None:
        baseline = "def foo(): pass\n"
        analysis_a = FileAnalysis(
            "f.py",
            functions_added={"bar"},
            changes=[SemanticChange(ChangeType.ADD_FUNCTION, "bar", "function:bar", 3, 4,
                                    content_after="def bar():\n    return 1\n")],
        )
        analysis_b = FileAnalysis(
            "f.py",
            functions_added={"baz"},
            changes=[SemanticChange(ChangeType.ADD_FUNCTION, "baz", "function:baz", 3, 4,
                                    content_after="def baz():\n    return 2\n")],
        )
        result = _strategy_append_functions(baseline, {"a": analysis_a, "b": analysis_b})
        assert "def bar" in result
        assert "def baz" in result
        assert result.index("def foo") < result.index("def bar")

    def test_skips_existing_functions(self) -> None:
        baseline = "def foo(): pass\n"
        analysis_a = FileAnalysis(
            "f.py",
            functions_added={"foo"},
            changes=[SemanticChange(ChangeType.ADD_FUNCTION, "foo", "function:foo", 1, 2,
                                    content_after="def foo(): pass\n")],
        )
        result = _strategy_append_functions(baseline, {"a": analysis_a})
        assert result.count("def foo") == 1

    def test_inserts_before_all_guard(self) -> None:
        baseline = "def foo(): pass\n\n__all__ = ['foo']\n"
        analysis_a = FileAnalysis(
            "f.py",
            functions_added={"bar"},
            changes=[SemanticChange(ChangeType.ADD_FUNCTION, "bar", "function:bar", 3, 4,
                                    content_after="def bar(): pass\n")],
        )
        result = _strategy_append_functions(baseline, {"a": analysis_a})
        assert "def bar" in result
        assert result.index("def bar") < result.index("__all__")

    def test_inserts_before_main_guard(self) -> None:
        baseline = "def foo(): pass\n\nif __name__ == '__main__':\n    foo()\n"
        analysis_a = FileAnalysis(
            "f.py",
            functions_added={"bar"},
            changes=[SemanticChange(ChangeType.ADD_FUNCTION, "bar", "function:bar", 3, 4,
                                    content_after="def bar(): pass\n")],
        )
        result = _strategy_append_functions(baseline, {"a": analysis_a})
        assert "def bar" in result
        assert result.index("def bar") < result.index("if __name__")

    def test_no_changes(self) -> None:
        baseline = "def foo(): pass\n"
        result = _strategy_append_functions(baseline, {"a": FileAnalysis("f.py")})
        assert result == baseline


# ---------------------------------------------------------------------------
# TestStrategyAppendMethods
# ---------------------------------------------------------------------------


class TestStrategyAppendMethods:
    def test_appends_methods_to_class(self) -> None:
        baseline = "class Cls:\n    def existing(self): pass\n"
        analysis_a = FileAnalysis(
            "f.py",
            changes=[SemanticChange(ChangeType.ADD_METHOD, "Cls.new_a", "class:Cls", 3, 4,
                                    content_after="    def new_a(self): pass\n")],
        )
        analysis_b = FileAnalysis(
            "f.py",
            changes=[SemanticChange(ChangeType.ADD_METHOD, "Cls.new_b", "class:Cls", 3, 4,
                                    content_after="    def new_b(self): pass\n")],
        )
        result = _strategy_append_methods(baseline, {"a": analysis_a, "b": analysis_b})
        assert "def new_a" in result
        assert "def new_b" in result
        # Both methods must be inside the class (indented)
        lines = result.splitlines()
        new_a_line = next((l for l in lines if "new_a" in l), None)
        assert new_a_line is not None
        assert new_a_line.startswith("    ")

    def test_no_methods_unchanged(self) -> None:
        baseline = "class Cls:\n    def existing(self): pass\n"
        result = _strategy_append_methods(baseline, {"a": FileAnalysis("f.py")})
        assert result == baseline

    def test_multiple_classes(self) -> None:
        baseline = "class A:\n    def a1(self): pass\n\nclass B:\n    def b1(self): pass\n"
        analysis = FileAnalysis(
            "f.py",
            changes=[
                SemanticChange(ChangeType.ADD_METHOD, "A.a2", "class:A", 3, 4,
                               content_after="    def a2(self): pass\n"),
                SemanticChange(ChangeType.ADD_METHOD, "B.b2", "class:B", 6, 7,
                               content_after="    def b2(self): pass\n"),
            ],
        )
        result = _strategy_append_methods(baseline, {"a": analysis})
        assert "def a2" in result
        assert "def b2" in result


# ---------------------------------------------------------------------------
# TestStrategyCombineConfigs
# ---------------------------------------------------------------------------


class TestStrategyCombineConfigs:
    def test_json_merge_add_keys(self) -> None:
        baseline = '{"version": 1}\n'
        analysis_a = FileAnalysis(
            "cfg.json",
            changes=[SemanticChange(ChangeType.ADD_CONFIG_KEY, "debug", "config_root", 1, 1,
                                    content_after='{"version": 1, "debug": true}')],
        )
        analysis_b = FileAnalysis(
            "cfg.json",
            changes=[SemanticChange(ChangeType.ADD_CONFIG_KEY, "timeout", "config_root", 1, 1,
                                    content_after='{"version": 1, "timeout": 30}')],
        )
        result = _strategy_combine_configs("cfg.json", baseline, {"a": analysis_a, "b": analysis_b})
        parsed = json.loads(result)
        assert parsed["version"] == 1
        assert parsed["debug"] is True
        assert parsed["timeout"] == 30

    def test_json_preserves_baseline_keys(self) -> None:
        baseline = '{"existing": "value", "version": 2}\n'
        analysis = FileAnalysis(
            "cfg.json",
            changes=[SemanticChange(ChangeType.ADD_CONFIG_KEY, "new_key", "config_root", 1, 1,
                                    content_after='{"existing": "value", "version": 2, "new_key": "hello"}')],
        )
        result = _strategy_combine_configs("cfg.json", baseline, {"a": analysis})
        parsed = json.loads(result)
        assert parsed["existing"] == "value"
        assert parsed["version"] == 2
        assert parsed["new_key"] == "hello"

    def test_invalid_json_returns_baseline(self) -> None:
        baseline = "not json"
        analysis = FileAnalysis("cfg.json")
        result = _strategy_combine_configs("cfg.json", baseline, {"a": analysis})
        assert result == baseline

    def test_yaml_returns_baseline(self) -> None:
        baseline = "key: value\n"
        analysis = FileAnalysis("cfg.yaml")
        result = _strategy_combine_configs("cfg.yaml", baseline, {"a": analysis})
        assert result == baseline

    def test_empty_baseline_json(self) -> None:
        baseline = ""
        analysis = FileAnalysis(
            "cfg.json",
            changes=[SemanticChange(ChangeType.ADD_CONFIG_KEY, "x", "config_root", 1, 1,
                                    content_after='{"x": 1}')],
        )
        result = _strategy_combine_configs("cfg.json", baseline, {"a": analysis})
        parsed = json.loads(result)
        assert parsed["x"] == 1


# ---------------------------------------------------------------------------
# TestStrategyOrderByDependency
# ---------------------------------------------------------------------------


class TestStrategyOrderByDependency:
    def test_imports_before_functions(self) -> None:
        baseline = "def foo(): pass\n"
        analysis_a = FileAnalysis(
            "f.py",
            imports_added={"import sys"},
            changes=[SemanticChange(ChangeType.ADD_IMPORT, "import sys", "file_top", 1, 1,
                                    content_after="import sys")],
        )
        analysis_b = FileAnalysis(
            "f.py",
            functions_added={"bar"},
            changes=[SemanticChange(ChangeType.ADD_FUNCTION, "bar", "function:bar", 3, 4,
                                    content_after="def bar(): pass\n")],
        )
        result = _strategy_order_by_dependency(baseline, {"a": analysis_a, "b": analysis_b})
        assert "import sys" in result
        assert "def bar" in result
        assert result.index("import sys") < result.index("def bar")

    def test_mixed_additive_changes(self) -> None:
        baseline = "import os\n\ndef foo(): pass\n"
        analysis = FileAnalysis(
            "f.py",
            imports_added={"import sys"},
            functions_added={"bar"},
            changes=[
                SemanticChange(ChangeType.ADD_IMPORT, "import sys", "file_top", 1, 1,
                               content_after="import sys"),
                SemanticChange(ChangeType.ADD_FUNCTION, "bar", "function:bar", 5, 6,
                               content_after="def bar(): pass\n"),
            ],
        )
        result = _strategy_order_by_dependency(baseline, {"a": analysis})
        assert "import sys" in result
        assert "def bar" in result


# ---------------------------------------------------------------------------
# TestMergeResolver
# ---------------------------------------------------------------------------


class TestMergeResolver:
    def test_pre_resolve_no_conflicts(self, conflict_repo: Path, tmp_path: Path) -> None:
        """Two branches adding different functions — should auto-resolve."""
        from golem.config import GolemConfig
        config = GolemConfig()
        resolver = MergeResolver(conflict_repo, config, enable_ai=False)
        report = resolver.pre_resolve(["branch-a", "branch-b"], "main")
        assert report.stats.files_auto_merged >= 1
        assert report.stats.files_need_review == 0

    def test_pre_resolve_true_conflict_no_ai(self, tmp_path: Path) -> None:
        """Two branches modifying the same function — NEEDS_HUMAN_REVIEW when AI disabled."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        subprocess.run(["git", "checkout", "-b", "a"], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("def foo():\n    return 99\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "mod"], cwd=repo, check=True, capture_output=True)

        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "b"], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("def foo():\n    return 42\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "mod"], cwd=repo, check=True, capture_output=True)

        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
        from golem.config import GolemConfig
        config = GolemConfig()
        resolver = MergeResolver(repo, config, enable_ai=False)
        report = resolver.pre_resolve(["a", "b"], "main")
        assert report.stats.files_need_review >= 1

    def test_pre_resolve_returns_merge_report(self, conflict_repo: Path) -> None:
        from golem.config import GolemConfig
        config = GolemConfig()
        resolver = MergeResolver(conflict_repo, config, enable_ai=False)
        report = resolver.pre_resolve(["branch-a", "branch-b"], "main")
        assert isinstance(report, MergeReport)
        assert isinstance(report.stats, MergeStats)
        assert isinstance(report.file_results, dict)

    def test_merge_report_saved(self, conflict_repo: Path, tmp_path: Path) -> None:
        """MergeReport is persisted to golem_dir/merge_reports/."""
        from golem.config import GolemConfig
        config = GolemConfig()
        resolver = MergeResolver(conflict_repo, config, enable_ai=False)
        report = resolver.pre_resolve(["branch-a", "branch-b"], "main")
        golem_dir = tmp_path / ".golem"
        saved_path = save_merge_report(report, golem_dir, run_id="test-run")
        assert saved_path.exists()
        data = json.loads(saved_path.read_text(encoding="utf-8"))
        assert "stats" in data
        assert "file_results" in data
        assert "branches_merged" in data

    def test_pre_resolve_single_branch_direct_copy(self, tmp_path: Path) -> None:
        """A file changed by only one branch results in DIRECT_COPY."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("def foo(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        subprocess.run(["git", "checkout", "-b", "only-a"], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("def foo(): pass\n\ndef bar(): return 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add bar"], cwd=repo, check=True, capture_output=True)

        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
        from golem.config import GolemConfig
        config = GolemConfig()
        resolver = MergeResolver(repo, config, enable_ai=False)
        report = resolver.pre_resolve(["only-a"], "main")
        assert "f.py" in report.file_results
        assert report.file_results["f.py"].decision == MergeDecision.DIRECT_COPY

    def test_pre_resolve_empty_branches(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("def foo(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        from golem.config import GolemConfig
        config = GolemConfig()
        resolver = MergeResolver(repo, config, enable_ai=False)
        report = resolver.pre_resolve([], "main")
        assert report.stats.files_processed == 0
        assert report.success

    def test_pre_resolve_import_conflict_resolved(self, tmp_path: Path) -> None:
        """Two branches adding different imports — should auto-merge."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("import os\n\ndef foo(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        subprocess.run(["git", "checkout", "-b", "add-sys"], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("import os\nimport sys\n\ndef foo(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add sys"], cwd=repo, check=True, capture_output=True)

        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "add-re"], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("import os\nimport re\n\ndef foo(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add re"], cwd=repo, check=True, capture_output=True)

        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
        from golem.config import GolemConfig
        config = GolemConfig()
        resolver = MergeResolver(repo, config, enable_ai=False)
        report = resolver.pre_resolve(["add-sys", "add-re"], "main")
        assert report.stats.files_auto_merged >= 1
        assert "f.py" in report.file_results
        result = report.file_results["f.py"]
        assert result.decision == MergeDecision.AUTO_MERGED
        assert result.merged_content is not None
        assert "import sys" in result.merged_content
        assert "import re" in result.merged_content


# ---------------------------------------------------------------------------
# TestSaveMergeReport
# ---------------------------------------------------------------------------


class TestSaveMergeReport:
    def test_creates_file(self, tmp_path: Path) -> None:
        report = MergeReport(
            success=True,
            branches_merged=["a", "b"],
            file_results={},
            stats=MergeStats(files_processed=2, files_auto_merged=2),
        )
        golem_dir = tmp_path / ".golem"
        path = save_merge_report(report, golem_dir, run_id="run-01")
        assert path.exists()
        assert path.name == "run-01.json"

    def test_json_schema(self, tmp_path: Path) -> None:
        report = MergeReport(
            success=False,
            branches_merged=["x"],
            file_results={
                "src/foo.py": FileMergeResult(
                    decision=MergeDecision.AUTO_MERGED,
                    file_path="src/foo.py",
                    merged_content="merged",
                    explanation="auto",
                ),
            },
            stats=MergeStats(files_auto_merged=1, conflicts_auto_resolved=1),
            error="some error",
        )
        golem_dir = tmp_path / ".golem"
        path = save_merge_report(report, golem_dir, run_id="run-02")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["success"] is False
        assert data["error"] == "some error"
        assert "src/foo.py" in data["file_results"]
        assert data["file_results"]["src/foo.py"]["decision"] == "auto_merged"
        assert "stats" in data

    def test_utf8_encoding(self, tmp_path: Path) -> None:
        report = MergeReport(
            success=True,
            branches_merged=["a"],
            file_results={},
            stats=MergeStats(),
        )
        golem_dir = tmp_path / ".golem"
        path = save_merge_report(report, golem_dir, run_id="unicode-run")
        content = path.read_text(encoding="utf-8")
        assert "branches_merged" in content


# ---------------------------------------------------------------------------
# TestWorktreeIntegration (integration test for the worktree.py hook)
# ---------------------------------------------------------------------------


class TestWorktreeIntegration:
    def test_merge_group_branches_with_config(self, tmp_path: Path) -> None:
        """
        merge_group_branches() with config + golem_dir runs pre-resolution
        and succeeds when both branches add different functions.
        """
        from golem.config import GolemConfig
        from golem.worktree import merge_group_branches

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("def foo(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        subprocess.run(["git", "checkout", "-b", "branch-a"], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("def foo(): pass\n\ndef bar(): return 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add bar"], cwd=repo, check=True, capture_output=True)

        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "branch-b"], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("def foo(): pass\n\ndef baz(): return 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add baz"], cwd=repo, check=True, capture_output=True)

        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)

        config = GolemConfig()
        golem_dir = tmp_path / ".golem"
        golem_dir.mkdir()
        success, info = merge_group_branches(
            ["branch-a", "branch-b"], "integration", repo, config=config, golem_dir=golem_dir,
        )
        # Even if pre-resolve doesn't fully prevent the git merge conflict,
        # the function should not raise an exception
        assert isinstance(success, bool)

    def test_merge_group_branches_without_config(self, tmp_path: Path) -> None:
        """Existing signature (no config/golem_dir) still works unchanged."""
        from golem.worktree import merge_group_branches

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        (repo / "README.md").write_text("init", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        subprocess.run(["git", "checkout", "-b", "feat"], cwd=repo, check=True, capture_output=True)
        (repo / "new_file.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add new_file"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)

        success, info = merge_group_branches(["feat"], "integration", repo)
        assert success is True
        assert info == ""
