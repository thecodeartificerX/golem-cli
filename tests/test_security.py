"""Tests for golem.security — denylist, validators, secret scanner, path containment."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from golem.security import (
    SecretMatch,
    SecurityAllowlist,
    _should_skip_file,
    extract_commands,
    is_path_contained,
    load_allowlist,
    mask_secret,
    scan_content,
    split_command_segments,
    validate_command,
    validate_dropdb,
    validate_git,
    validate_kill,
    validate_pkill,
    validate_psql,
    validate_rm,
    validate_shell_c,
    validate_write_content,
)


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------


class TestExtractCommands:
    def test_simple(self) -> None:
        assert extract_commands("ls -la") == ["ls"]

    def test_pipe(self) -> None:
        assert extract_commands("cat foo | grep bar") == ["cat", "grep"]

    def test_and_chain(self) -> None:
        cmds = extract_commands("cd /tmp && rm -rf old && ls")
        assert cmds == ["cd", "rm", "ls"]

    def test_semicolon_chain(self) -> None:
        cmds = extract_commands("echo hello; sudo shutdown now")
        assert "echo" in cmds
        assert "sudo" in cmds

    def test_var_assignment_skipped(self) -> None:
        cmds = extract_commands("FOO=bar baz --flag")
        assert cmds == ["baz"]

    def test_empty_returns_empty(self) -> None:
        assert extract_commands("") == []

    def test_whitespace_only_returns_empty(self) -> None:
        assert extract_commands("   ") == []

    def test_windows_path_fallback(self) -> None:
        # Should not crash on Windows paths
        cmds = extract_commands(r"C:\Windows\system32\cmd.exe /c dir")
        # cmd fallback may return "cmd" or empty list — either is acceptable
        assert isinstance(cmds, list)

    def test_shell_bypass(self) -> None:
        # bash -c should surface 'bash' as command for validator to catch
        cmds = extract_commands("bash -c 'sudo rm -rf /'")
        assert "bash" in cmds

    def test_or_chain(self) -> None:
        cmds = extract_commands("test -f foo || echo missing")
        assert "test" in cmds
        assert "echo" in cmds

    def test_path_basename(self) -> None:
        cmds = extract_commands("/usr/bin/python3 script.py")
        assert "python3" in cmds

    def test_multiple_pipes(self) -> None:
        cmds = extract_commands("cat file | sort | uniq | head")
        assert "cat" in cmds
        assert "sort" in cmds
        assert "uniq" in cmds
        assert "head" in cmds


class TestSplitCommandSegments:
    def test_and_split(self) -> None:
        segs = split_command_segments("git add . && git commit -m 'msg'")
        assert len(segs) == 2
        assert "git add ." in segs[0]
        assert "git commit" in segs[1]

    def test_semicolon_split(self) -> None:
        segs = split_command_segments("echo a; echo b")
        assert len(segs) == 2

    def test_or_split(self) -> None:
        segs = split_command_segments("cmd1 || cmd2")
        assert len(segs) == 2

    def test_single_command(self) -> None:
        segs = split_command_segments("ls -la")
        assert segs == ["ls -la"]

    def test_empty(self) -> None:
        assert split_command_segments("") == []


# ---------------------------------------------------------------------------
# Denylist
# ---------------------------------------------------------------------------


class TestDenylist:
    def test_blocked_sudo(self) -> None:
        ok, reason = validate_command("sudo apt install foo")
        assert not ok
        assert "sudo" in reason

    def test_blocked_shutdown(self) -> None:
        ok, _ = validate_command("shutdown -h now")
        assert not ok

    def test_blocked_reboot(self) -> None:
        ok, _ = validate_command("reboot")
        assert not ok

    def test_blocked_dd(self) -> None:
        ok, _ = validate_command("dd if=/dev/zero of=/dev/sda")
        assert not ok

    def test_blocked_mkfs(self) -> None:
        ok, _ = validate_command("mkfs.ext4 /dev/sdb1")
        assert not ok

    def test_blocked_systemctl(self) -> None:
        ok, _ = validate_command("systemctl stop nginx")
        assert not ok

    def test_blocked_useradd(self) -> None:
        ok, _ = validate_command("useradd newuser")
        assert not ok

    def test_blocked_iptables(self) -> None:
        ok, _ = validate_command("iptables -A INPUT -j DROP")
        assert not ok

    def test_blocked_in_chain(self) -> None:
        ok, _ = validate_command("echo hi && sudo reboot")
        assert not ok

    def test_blocked_in_pipe(self) -> None:
        ok, _ = validate_command("cat /etc/passwd | sudo tee /etc/shadow")
        assert not ok

    def test_allowed_normal(self) -> None:
        ok, _ = validate_command("uv run pytest")
        assert ok

    def test_allowed_git_status(self) -> None:
        ok, _ = validate_command("git status")
        assert ok

    def test_allowed_echo(self) -> None:
        ok, _ = validate_command("echo hello world")
        assert ok

    def test_allowed_python(self) -> None:
        ok, _ = validate_command("python3 --version")
        assert ok

    def test_empty_command_allowed(self) -> None:
        ok, _ = validate_command("")
        assert ok

    def test_whitespace_only_allowed(self) -> None:
        ok, _ = validate_command("   ")
        assert ok

    def test_blocked_net_windows(self) -> None:
        ok, _ = validate_command("net user administrator /add")
        assert not ok

    def test_blocked_schtasks_windows(self) -> None:
        ok, _ = validate_command("schtasks /create /tn evil")
        assert not ok

    def test_blocked_diskpart_windows(self) -> None:
        ok, _ = validate_command("diskpart")
        assert not ok


# ---------------------------------------------------------------------------
# rm validator
# ---------------------------------------------------------------------------


class TestValidateRm:
    def test_safe_file(self) -> None:
        ok, _ = validate_rm("rm -rf /tmp/golem-test")
        assert ok

    def test_safe_relative(self) -> None:
        ok, _ = validate_rm("rm -f ./dist/bundle.js")
        assert ok

    def test_block_root(self) -> None:
        ok, _ = validate_rm("rm -rf /")
        assert not ok

    def test_block_no_preserve_root(self) -> None:
        ok, _ = validate_rm("rm --no-preserve-root /")
        assert not ok

    def test_block_home_tilde(self) -> None:
        ok, _ = validate_rm("rm -rf ~")
        assert not ok

    def test_block_etc(self) -> None:
        ok, _ = validate_rm("rm -rf /etc")
        assert not ok

    def test_block_usr(self) -> None:
        ok, _ = validate_rm("rm -rf /usr")
        assert not ok

    def test_block_var(self) -> None:
        ok, _ = validate_rm("rm -rf /var")
        assert not ok

    def test_block_parent_traverse(self) -> None:
        ok, _ = validate_rm("rm -rf ../secret")
        assert not ok

    def test_block_bare_wildcard(self) -> None:
        ok, _ = validate_rm("rm -rf *")
        assert not ok

    def test_block_root_wildcard(self) -> None:
        ok, _ = validate_rm("rm -rf /*")
        assert not ok

    def test_allow_nested_tmp(self) -> None:
        ok, _ = validate_rm("rm -rf /tmp/something/nested")
        assert ok

    def test_allow_flags_only(self) -> None:
        # No path tokens — flags only should pass (rm alone is valid)
        ok, _ = validate_rm("rm -f")
        assert ok


# ---------------------------------------------------------------------------
# git validator
# ---------------------------------------------------------------------------


class TestValidateGit:
    def test_allow_commit(self) -> None:
        ok, _ = validate_git("git commit -m 'fix: something'")
        assert ok

    def test_allow_add(self) -> None:
        ok, _ = validate_git("git add .")
        assert ok

    def test_allow_status(self) -> None:
        ok, _ = validate_git("git status")
        assert ok

    def test_allow_fetch(self) -> None:
        ok, _ = validate_git("git fetch origin")
        assert ok

    def test_allow_push_with_lease(self) -> None:
        ok, _ = validate_git("git push --force-with-lease")
        assert ok

    def test_allow_pull(self) -> None:
        ok, _ = validate_git("git pull origin main")
        assert ok

    def test_allow_log(self) -> None:
        ok, _ = validate_git("git log --oneline")
        assert ok

    def test_allow_diff(self) -> None:
        ok, _ = validate_git("git diff HEAD")
        assert ok

    def test_block_force_push(self) -> None:
        ok, reason = validate_git("git push --force")
        assert not ok
        assert "force" in reason.lower()

    def test_block_push_f(self) -> None:
        ok, _ = validate_git("git push -f origin main")
        assert not ok

    def test_block_reset_hard(self) -> None:
        ok, _ = validate_git("git reset --hard HEAD~1")
        assert not ok

    def test_block_checkout_discard_files(self) -> None:
        ok, _ = validate_git("git checkout -- .")
        assert not ok

    def test_block_checkout_dot(self) -> None:
        ok, _ = validate_git("git checkout .")
        assert not ok

    def test_block_checkout_force(self) -> None:
        ok, _ = validate_git("git checkout -f mybranch")
        assert not ok

    def test_block_branch_D(self) -> None:
        ok, _ = validate_git("git branch -D feature/old")
        assert not ok

    def test_allow_branch_d_lowercase(self) -> None:
        ok, _ = validate_git("git branch -d feature/merged")
        assert ok

    def test_block_stash(self) -> None:
        ok, _ = validate_git("git stash")
        assert not ok

    def test_block_config_user_email(self) -> None:
        ok, reason = validate_git("git config user.email 'test@test.com'")
        assert not ok
        assert "user.email" in reason

    def test_block_config_user_name(self) -> None:
        ok, reason = validate_git("git config user.name 'Test User'")
        assert not ok
        assert "user.name" in reason

    def test_block_config_credential_helper(self) -> None:
        ok, _ = validate_git("git config credential.helper store")
        assert not ok

    def test_allow_config_list(self) -> None:
        ok, _ = validate_git("git config --list")
        assert ok

    def test_allow_config_get(self) -> None:
        ok, _ = validate_git("git config --get user.email")
        assert ok

    def test_block_inline_config_c(self) -> None:
        ok, reason = validate_git("git -c user.email=evil@x.com commit -m 'hi'")
        assert not ok
        assert "user.email" in reason

    def test_block_inline_config_c_nospace(self) -> None:
        ok, reason = validate_git("git -cuser.name=Evil commit -m 'hi'")
        assert not ok
        assert "user.name" in reason

    def test_block_interactive_rebase(self) -> None:
        ok, _ = validate_git("git rebase -i HEAD~3")
        assert not ok

    def test_block_interactive_add(self) -> None:
        ok, _ = validate_git("git add -p")
        assert not ok

    def test_block_interactive_add_i(self) -> None:
        ok, _ = validate_git("git add -i")
        assert not ok

    def test_allow_non_git_passthrough(self) -> None:
        # validate_git called with non-git segment should pass
        ok, _ = validate_git("ls -la")
        assert ok

    def test_block_clean_force(self) -> None:
        ok, _ = validate_git("git clean -f")
        assert not ok

    def test_block_clean_force_long(self) -> None:
        ok, _ = validate_git("git clean --force -d")
        assert not ok


# ---------------------------------------------------------------------------
# Process validators
# ---------------------------------------------------------------------------


class TestValidateKill:
    def test_allow_by_pid(self) -> None:
        ok, _ = validate_kill("kill -9 12345")
        assert ok

    def test_allow_sigterm(self) -> None:
        ok, _ = validate_kill("kill 54321")
        assert ok

    def test_block_broadcast(self) -> None:
        ok, _ = validate_kill("kill -1")
        assert not ok

    def test_block_zero(self) -> None:
        ok, _ = validate_kill("kill 0")
        assert not ok

    def test_block_dash_zero(self) -> None:
        ok, _ = validate_kill("kill -0")
        assert not ok


class TestValidatePkill:
    def test_allow_app_process(self) -> None:
        ok, _ = validate_pkill("pkill -f myapp")
        assert ok

    def test_allow_named_process(self) -> None:
        ok, _ = validate_pkill("pkill uvicorn")
        assert ok

    def test_block_systemd(self) -> None:
        ok, reason = validate_pkill("pkill systemd")
        assert not ok
        assert "systemd" in reason

    def test_block_claude(self) -> None:
        ok, reason = validate_pkill("pkill claude")
        assert not ok
        assert "claude" in reason

    def test_block_user_flag(self) -> None:
        ok, _ = validate_pkill("pkill -u root")
        assert not ok

    def test_block_user_flag_long(self) -> None:
        ok, _ = validate_pkill("pkill --euid root")
        assert not ok

    def test_block_no_process_name(self) -> None:
        ok, _ = validate_pkill("pkill -9")
        assert not ok

    def test_block_launchd(self) -> None:
        ok, _ = validate_pkill("pkill launchd")
        assert not ok


# ---------------------------------------------------------------------------
# Shell interpreter bypass validator
# ---------------------------------------------------------------------------


class TestValidateShellC:
    def test_block_sudo_in_bash_c(self) -> None:
        ok, _ = validate_command("bash -c 'sudo rm -rf /'")
        assert not ok

    def test_block_shutdown_in_sh_c(self) -> None:
        ok, _ = validate_command("sh -c 'shutdown now'")
        assert not ok

    def test_allow_safe_bash_c(self) -> None:
        ok, _ = validate_command("bash -c 'echo hello'")
        assert ok

    def test_allow_bash_version(self) -> None:
        ok, _ = validate_command("bash --version")
        assert ok

    def test_block_nested_shell(self) -> None:
        ok, _ = validate_command("bash -c \"sh -c 'sudo reboot'\"")
        assert not ok

    def test_block_process_substitution(self) -> None:
        ok, _ = validate_shell_c("bash <(cat evil)")
        assert not ok

    def test_validate_shell_c_directly(self) -> None:
        ok, _ = validate_shell_c("bash -c 'echo safe'")
        assert ok

    def test_block_via_validate_command(self) -> None:
        ok, _ = validate_command("sh -c 'useradd hacker'")
        assert not ok


# ---------------------------------------------------------------------------
# Database validators
# ---------------------------------------------------------------------------


class TestValidatePsql:
    def test_allow_select(self) -> None:
        ok, _ = validate_psql("psql -c 'SELECT * FROM users'")
        assert ok

    def test_allow_insert(self) -> None:
        ok, _ = validate_psql("psql -c 'INSERT INTO logs VALUES (1, now())'")
        assert ok

    def test_block_drop_table(self) -> None:
        ok, reason = validate_psql("psql -c 'DROP TABLE users'")
        assert not ok
        assert "destructive" in reason.lower()

    def test_block_truncate(self) -> None:
        ok, _ = validate_psql("psql -c 'TRUNCATE TABLE logs'")
        assert not ok

    def test_block_drop_database(self) -> None:
        ok, _ = validate_psql("psql -c 'DROP DATABASE myapp'")
        assert not ok

    def test_allow_no_sql_arg(self) -> None:
        ok, _ = validate_psql("psql --host localhost mydb")
        assert ok


class TestValidateDropdb:
    def test_allow_test_db(self) -> None:
        ok, _ = validate_dropdb("dropdb test_myapp")
        assert ok

    def test_allow_dev_db(self) -> None:
        ok, _ = validate_dropdb("dropdb myapp_dev")
        assert ok

    def test_allow_local_db(self) -> None:
        ok, _ = validate_dropdb("dropdb myapp_local")
        assert ok

    def test_allow_scratch_db(self) -> None:
        ok, _ = validate_dropdb("dropdb scratch")
        assert ok

    def test_allow_sandbox_db(self) -> None:
        ok, _ = validate_dropdb("dropdb sandbox_db")
        assert ok

    def test_block_production_db(self) -> None:
        ok, reason = validate_dropdb("dropdb myapp_production")
        assert not ok
        assert "blocked" in reason.lower()

    def test_block_staging_db(self) -> None:
        ok, _ = validate_dropdb("dropdb myapp_staging")
        assert not ok

    def test_block_no_db_name(self) -> None:
        ok, _ = validate_dropdb("dropdb -h localhost")
        assert not ok

    def test_allow_with_flags(self) -> None:
        ok, _ = validate_dropdb("dropdb -h localhost -p 5432 test_myapp")
        assert ok


# ---------------------------------------------------------------------------
# Path containment
# ---------------------------------------------------------------------------


class TestPathContainment:
    def test_contained_relative(self, tmp_path: Path) -> None:
        ok, _ = is_path_contained("src/foo.py", str(tmp_path))
        assert ok

    def test_contained_absolute(self, tmp_path: Path) -> None:
        ok, _ = is_path_contained(str(tmp_path / "src" / "foo.py"), str(tmp_path))
        assert ok

    def test_not_contained_traversal(self, tmp_path: Path) -> None:
        ok, reason = is_path_contained("../../etc/passwd", str(tmp_path))
        assert not ok
        assert "outside" in reason

    def test_not_contained_absolute_external(self, tmp_path: Path) -> None:
        ok, _ = is_path_contained("/etc/passwd", str(tmp_path))
        assert not ok

    def test_prefix_confusion_attack(self, tmp_path: Path) -> None:
        # /tmp/project should not match /tmp/project-evil
        safe_dir = tmp_path / "project"
        evil_dir = tmp_path / "project-evil"
        safe_dir.mkdir()
        evil_dir.mkdir()
        ok, _ = is_path_contained(str(evil_dir / "file.py"), str(safe_dir))
        assert not ok

    def test_root_itself_is_contained(self, tmp_path: Path) -> None:
        ok, _ = is_path_contained(str(tmp_path), str(tmp_path))
        assert ok

    def test_deeply_nested_contained(self, tmp_path: Path) -> None:
        ok, _ = is_path_contained(str(tmp_path / "a" / "b" / "c" / "d.txt"), str(tmp_path))
        assert ok


# ---------------------------------------------------------------------------
# Secret scanner
# ---------------------------------------------------------------------------


class TestScanContent:
    def test_detect_openai_key(self) -> None:
        content = 'API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"'
        matches = scan_content(content, "config.py")
        assert len(matches) >= 1
        assert any("OpenAI" in m.pattern_name or "API key" in m.pattern_name for m in matches)

    def test_detect_anthropic_key(self) -> None:
        content = 'key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"'
        matches = scan_content(content, "config.py")
        assert any("Anthropic" in m.pattern_name for m in matches)

    def test_detect_aws_access_key(self) -> None:
        # Real-looking AWS access key (not doc placeholder with EXAMPLE)
        content = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7PRODKEY"
        matches = scan_content(content, ".env")
        assert any("AWS" in m.pattern_name for m in matches)

    def test_detect_aws_secret_key(self) -> None:
        # 40-char AWS-style secret (no false positive triggers in value)
        content = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYabcdefghijk"
        matches = scan_content(content, ".env")
        assert any("AWS" in m.pattern_name for m in matches)

    def test_detect_rsa_private_key(self) -> None:
        content = "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."
        matches = scan_content(content, "key.pem")
        assert any("Private Key" in m.pattern_name for m in matches)

    def test_detect_openssh_private_key(self) -> None:
        content = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC..."
        matches = scan_content(content, "id_rsa")
        assert any("Private Key" in m.pattern_name for m in matches)

    def test_detect_github_pat(self) -> None:
        content = 'token = "ghp_abcdefghijklmnopqrstuvwxyz123456789012"'
        matches = scan_content(content, "config.py")
        assert any("GitHub" in m.pattern_name for m in matches)

    def test_detect_jwt(self) -> None:
        content = (
            'token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.'
            'eyJzdWIiOiIxMjM0NTY3ODkwIn0.'
            'dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"'
        )
        matches = scan_content(content, "auth.py")
        assert any("JWT" in m.pattern_name for m in matches)

    def test_detect_stripe_live_key(self) -> None:
        content = 'STRIPE_KEY = "sk_live_abcdefghijklmnopqrstuvwx"'
        matches = scan_content(content, "payment.py")
        assert any("Stripe" in m.pattern_name for m in matches)

    def test_detect_slack_bot_token(self) -> None:
        content = 'SLACK_TOKEN = "xoxb-123456789-123456789-abcdefghijklmnopqrstuvwx"'
        matches = scan_content(content, "notifications.py")
        assert any("Slack" in m.pattern_name for m in matches)

    def test_false_positive_env_ref(self) -> None:
        content = 'API_KEY = os.environ["OPENAI_API_KEY"]'
        matches = scan_content(content, "config.py")
        assert len(matches) == 0

    def test_false_positive_placeholder(self) -> None:
        content = 'API_KEY = "your-api-key-here"'
        matches = scan_content(content, "config.py")
        assert len(matches) == 0

    def test_false_positive_example_in_comment(self) -> None:
        content = "# Example: sk-example-key-not-real"
        matches = scan_content(content, "docs.py")
        assert len(matches) == 0

    def test_false_positive_xxx(self) -> None:
        content = 'token = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"'
        matches = scan_content(content, "test.py")
        assert len(matches) == 0

    def test_false_positive_changeme(self) -> None:
        content = 'SECRET = "CHANGEME_this_is_not_real"'
        matches = scan_content(content, "config.py")
        assert len(matches) == 0

    def test_multiline_scan(self) -> None:
        content = "x = 1\nAWS_ACCESS_KEY_ID=AKIAIOSFODNN7PRODKEY\ny = 2"
        matches = scan_content(content, ".env")
        assert len(matches) >= 1
        assert matches[0].line_number == 2

    def test_secret_match_fields(self) -> None:
        content = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7PRODKEY"
        matches = scan_content(content, "test.env")
        assert len(matches) >= 1
        m = matches[0]
        assert m.file_path == "test.env"
        assert m.line_number == 1
        assert isinstance(m.matched_text, str)
        assert isinstance(m.line_content, str)
        assert len(m.line_content) <= 100


class TestMaskSecret:
    def test_long_secret_masked(self) -> None:
        assert mask_secret("sk-abcdefghijklmnop") == "sk-abcde***"

    def test_short_secret_not_masked(self) -> None:
        assert mask_secret("short") == "short"

    def test_exactly_8_chars(self) -> None:
        assert mask_secret("12345678") == "12345678"

    def test_9_chars_masked(self) -> None:
        result = mask_secret("123456789")
        assert result == "12345678***"

    def test_custom_visible_chars(self) -> None:
        result = mask_secret("abcdefghijklmnop", visible_chars=4)
        assert result == "abcd***"


class TestShouldSkipFile:
    def test_skip_md(self) -> None:
        assert _should_skip_file("README.md") is True

    def test_skip_txt(self) -> None:
        assert _should_skip_file("notes.txt") is True

    def test_skip_rst(self) -> None:
        assert _should_skip_file("docs/api.rst") is True

    def test_skip_node_modules(self) -> None:
        assert _should_skip_file("node_modules/foo/bar.js") is True

    def test_skip_venv(self) -> None:
        assert _should_skip_file(".venv/lib/python3.12/site-packages/foo.py") is True

    def test_skip_binary_png(self) -> None:
        assert _should_skip_file("logo.png") is True

    def test_skip_example_file(self) -> None:
        assert _should_skip_file("config.example") is True

    def test_skip_lock_file(self) -> None:
        assert _should_skip_file("poetry.lock") is True

    def test_not_skip_py(self) -> None:
        assert _should_skip_file("src/config.py") is False

    def test_not_skip_env(self) -> None:
        assert _should_skip_file(".env") is False

    def test_not_skip_json(self) -> None:
        assert _should_skip_file("config.json") is False


# ---------------------------------------------------------------------------
# validate_write_content
# ---------------------------------------------------------------------------


class TestValidateWriteContent:
    def test_clean_write_allowed(self) -> None:
        ok, _ = validate_write_content("x = 1", "src/foo.py")
        assert ok

    def test_secret_write_blocked(self) -> None:
        content = 'OPENAI_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"'
        ok, reason = validate_write_content(content, "src/config.py")
        assert not ok
        assert "Secret detected" in reason
        assert "sk-abcde***" in reason  # masked

    def test_md_file_skipped(self) -> None:
        content = 'key = "sk-abcdefghijklmnopqrstuvwxyz123456"'
        ok, _ = validate_write_content(content, "docs/README.md")
        assert ok

    def test_private_key_blocked(self) -> None:
        content = "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."
        ok, reason = validate_write_content(content, "certs/server.key")
        assert not ok
        assert "Secret detected" in reason

    def test_allowlist_bypasses_write(self) -> None:
        content = 'OPENAI_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"'
        al = SecurityAllowlist(commands=["write:src/config.py"])
        ok, _ = validate_write_content(content, "src/config.py", allowlist=al)
        assert ok

    def test_allowlist_wrong_path_still_blocked(self) -> None:
        content = 'OPENAI_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"'
        al = SecurityAllowlist(commands=["write:src/other.py"])
        ok, _ = validate_write_content(content, "src/config.py", allowlist=al)
        assert not ok

    def test_max_5_matches_shown(self) -> None:
        # Construct content with many secrets
        aws_keys = "\n".join(f"AWS_KEY_{i}=AKIA{'A' * 16}" for i in range(10))
        ok, reason = validate_write_content(aws_keys, "keys.env")
        assert not ok
        assert "... and more" in reason


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_allowlisted_command_passes(self) -> None:
        al = SecurityAllowlist(commands=["dropdb test_myapp"])
        ok, _ = validate_command("dropdb test_myapp", allowlist=al)
        assert ok

    def test_non_allowlisted_still_blocked(self) -> None:
        al = SecurityAllowlist(commands=["dropdb test_myapp"])
        ok, _ = validate_command("sudo rm -rf /", allowlist=al)
        assert not ok

    def test_empty_allowlist(self) -> None:
        al = SecurityAllowlist()
        ok, _ = validate_command("sudo rm -rf /", allowlist=al)
        assert not ok

    def test_allowlist_strips_whitespace(self) -> None:
        al = SecurityAllowlist(commands=["  dropdb test_myapp  "])
        assert al.allows("dropdb test_myapp")

    def test_allowlist_exact_match(self) -> None:
        al = SecurityAllowlist(commands=["dropdb test_myapp"])
        assert not al.allows("dropdb test_other")

    def test_allowlist_case_sensitive(self) -> None:
        al = SecurityAllowlist(commands=["dropdb test_myapp"])
        assert not al.allows("DROPDB test_myapp")


class TestLoadAllowlist:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        al = load_allowlist(tmp_path, "nonexistent-session")
        assert al.commands == []

    def test_loads_commands_from_file(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "sessions" / "test-session"
        session_dir.mkdir(parents=True)
        allowlist_file = session_dir / "security_allowlist.json"
        allowlist_file.write_text(
            json.dumps({"commands": ["dropdb test_myapp", "git push --force-with-lease"]}),
            encoding="utf-8",
        )
        al = load_allowlist(tmp_path, "test-session")
        assert "dropdb test_myapp" in al.commands
        assert "git push --force-with-lease" in al.commands

    def test_missing_commands_key_returns_empty(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "sessions" / "test-session"
        session_dir.mkdir(parents=True)
        allowlist_file = session_dir / "security_allowlist.json"
        allowlist_file.write_text(json.dumps({}), encoding="utf-8")
        al = load_allowlist(tmp_path, "test-session")
        assert al.commands == []


# ---------------------------------------------------------------------------
# Integration: end-to-end validate_command
# ---------------------------------------------------------------------------


class TestValidateCommandIntegration:
    def test_sudo_in_compound_blocked(self) -> None:
        ok, _ = validate_command("git status && sudo apt update")
        assert not ok

    def test_complex_safe_command(self) -> None:
        ok, _ = validate_command("cd /tmp && uv run pytest tests/ -v --tb=short")
        assert ok

    def test_git_force_push_via_validate_command(self) -> None:
        ok, _ = validate_command("git push --force origin main")
        assert not ok

    def test_rm_dangerous_via_validate_command(self) -> None:
        ok, _ = validate_command("rm -rf /")
        assert not ok

    def test_rm_safe_via_validate_command(self) -> None:
        ok, _ = validate_command("rm -rf /tmp/build")
        assert ok

    def test_psql_destructive_via_validate_command(self) -> None:
        ok, _ = validate_command("psql mydb -c 'DROP TABLE users'")
        assert not ok

    def test_dropdb_safe_via_validate_command(self) -> None:
        ok, _ = validate_command("dropdb test_myapp")
        assert ok

    def test_dropdb_unsafe_via_validate_command(self) -> None:
        ok, _ = validate_command("dropdb production_myapp")
        assert not ok
