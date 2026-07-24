"""github_setup.sh / github_backup.sh / github_release.sh 的集成测试。

在临时目录中搭建真实布局并用 bash 实测：
    repo/           临时 git 工作仓库（含 scripts/ 三个被测脚本、app/ 代码目录）
    origin.git      本地 bare 仓库，以 file:// 协议作为 origin（真实测试 push！）
    home/           临时 HOME，隔离 git 全局配置（写入测试专用 user.name/email）

覆盖：
- github_setup.sh     添加 remote 成功，git remote -v 可见
- github_backup.sh    app/ 文件变更 -> bare 仓库收到提交与 backup/* 标签；
                      无变更时优雅跳过（返回 0 且不再产生新提交/标签）
- github_release.sh   0.1.0 -> bare 仓库收到 v0.1.0 注解标签（自动补 v 前缀）
- 失败路径            未配置 remote 时 github_backup.sh 返回非 0 且有诊断信息

兼容两种运行方式：
    python -m unittest tests.test_github_scripts
    python tests/test_github_scripts.py
（pytest 直接收集亦可）
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 仓库根目录（app/tests/ 的上两级），用于定位被测脚本
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

TEST_SCRIPTS = ["github_setup.sh", "github_backup.sh", "github_release.sh"]


def _git(args, cwd=None, env=None, check=True):
    """执行 git 命令，返回 CompletedProcess；默认失败即抛异常。"""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


class GitHubScriptsTestCase(unittest.TestCase):
    """每个用例独立搭建临时仓库 + bare origin + 临时 HOME。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agenelf-github-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        # 临时 HOME：写入测试专用 git 全局配置，隔离宿主机环境
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.home / ".gitconfig").write_text(
            '[user]\n\tname = 测试机器人\n\temail = test@example.com\n'
            '[init]\n\tdefaultBranch = main\n',
            encoding="utf-8",
        )
        self.env = dict(os.environ)
        self.env["HOME"] = str(self.home)
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"   # 忽略系统级 git 配置
        self.env["GIT_TERMINAL_PROMPT"] = "0"   # 禁止凭据交互提示

        # 本地 bare 仓库作为 origin（file:// 协议，真实走 push 路径）
        self.bare = self.tmp / "origin.git"
        _git(["init", "--bare", "-b", "main", str(self.bare)], env=self.env)

    # ---------- 辅助方法 ----------

    def _make_repo(self, name="repo", with_remote=True):
        """创建临时工作仓库：复制被测脚本、造 app/ 初始文件并提交。"""
        repo = self.tmp / name
        (repo / "scripts").mkdir(parents=True)
        (repo / "app").mkdir()
        for s in TEST_SCRIPTS:
            shutil.copy(SCRIPTS_DIR / s, repo / "scripts" / s)
        # logs/ 由脚本运行时生成，忽略之，保持 git status 干净
        (repo / ".gitignore").write_text("logs/\n", encoding="utf-8")
        (repo / "app" / "hello.py").write_text(
            '"""占位模块"""\n\n\ndef hello():\n    return "你好"\n',
            encoding="utf-8",
        )
        _git(["init", "-b", "main"], cwd=repo, env=self.env)
        _git(["add", "-A"], cwd=repo, env=self.env)
        _git(
            ["-c", "user.name=测试机器人", "-c", "user.email=test@example.com",
             "commit", "-m", "chore: 初始提交"],
            cwd=repo, env=self.env,
        )
        if with_remote:
            _git(["remote", "add", "origin", f"file://{self.bare}"],
                 cwd=repo, env=self.env)
        return repo

    def _run_script(self, repo, script, *args):
        """以 bash 运行被测脚本，返回 CompletedProcess（不抛异常）。"""
        git_bash = Path(os.environ.get("PROGRAMFILES", r"C:\\Program Files")) / "Git" / "bin" / "bash.exe"
        bash = str(git_bash) if os.name == "nt" and git_bash.is_file() else "bash"
        return subprocess.run(
            [bash, f"scripts/{script}", *args],
            cwd=repo,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def _bare(self, *args, check=True):
        """在 bare 仓库上执行 git 命令。"""
        return _git(["--git-dir", str(self.bare), *args], env=self.env, check=check)

    # ---------- 用例 ----------

    def test_setup_adds_remote(self):
        """github_setup.sh 添加 remote 成功，git remote -v 可见。"""
        repo = self.tmp / "repo-setup"
        (repo / "scripts").mkdir(parents=True)
        shutil.copy(SCRIPTS_DIR / "github_setup.sh", repo / "scripts")
        _git(["init", "-b", "main"], cwd=repo, env=self.env)

        url = f"file://{self.bare}"
        r = self._run_script(repo, "github_setup.sh", url)
        self.assertEqual(r.returncode, 0, msg=r.stderr + r.stdout)
        self.assertIn("已添加 origin", r.stdout)

        remotes = _git(["remote", "-v"], cwd=repo, env=self.env).stdout
        self.assertIn("origin", remotes)
        self.assertIn(url.replace("\\", "/"), remotes)

    def test_setup_help(self):
        """github_setup.sh --help 正常输出帮助并返回 0。"""
        repo = self.tmp / "repo-help"
        (repo / "scripts").mkdir(parents=True)
        shutil.copy(SCRIPTS_DIR / "github_setup.sh", repo / "scripts")
        r = self._run_script(repo, "github_setup.sh", "--help")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("用法", r.stdout)

    def test_backup_pushes_commit_and_tag(self):
        """app/ 变更 -> bare 仓库收到提交与 backup/* 标签。"""
        repo = self._make_repo()
        # 造一个 app/ 文件变更
        (repo / "app" / "hello.py").write_text(
            '"""占位模块 v2"""\n\n\ndef hello():\n    return "你好，世界"\n',
            encoding="utf-8",
        )
        r = self._run_script(repo, "github_backup.sh", "backup: 测试备份")
        self.assertEqual(r.returncode, 0, msg=r.stderr + r.stdout)

        # bare 仓库收到提交：main 指向本地 HEAD，且提交信息一致
        local_head = _git(["rev-parse", "HEAD"], cwd=repo, env=self.env).stdout.strip()
        bare_head = self._bare("rev-parse", "main^{commit}").stdout.strip()
        self.assertEqual(local_head, bare_head)
        bare_msg = self._bare("log", "-1", "--format=%s", "main").stdout.strip()
        self.assertEqual(bare_msg, "backup: 测试备份")

        # bare 仓库收到 backup/* 标签
        tags = self._bare("tag", "-l", "backup/*").stdout.split()
        self.assertEqual(len(tags), 1, msg=f"backup 标签数量异常：{tags}")

        # 日志已追加 logs/github.log
        log_text = (repo / "logs" / "github.log").read_text(encoding="utf-8")
        self.assertIn("推送完成", log_text)

    def test_backup_no_changes_skips_gracefully(self):
        """无变更时运行应优雅跳过：返回 0、有提示、不产生新提交。"""
        repo = self._make_repo()
        before = _git(["rev-parse", "HEAD"], cwd=repo, env=self.env).stdout.strip()
        r = self._run_script(repo, "github_backup.sh")
        self.assertEqual(r.returncode, 0, msg=r.stderr + r.stdout)
        self.assertIn("无变更", r.stdout)
        after = _git(["rev-parse", "HEAD"], cwd=repo, env=self.env).stdout.strip()
        self.assertEqual(before, after)

    def test_release_creates_annotated_tag(self):
        """github_release.sh 0.1.0 -> bare 仓库收到 v0.1.0 注解标签。"""
        repo = self._make_repo()
        # 造一份 evolution.log 供发布说明引用
        (repo / "logs").mkdir(exist_ok=True)
        (repo / "logs" / "evolution.log").write_text(
            "[promote] ===== 晋升完成：req-001 =====\n", encoding="utf-8"
        )

        r = self._run_script(repo, "github_release.sh", "0.1.0")
        self.assertEqual(r.returncode, 0, msg=r.stderr + r.stdout)

        # bare 仓库收到 v0.1.0（自动补 v 前缀）
        tags = self._bare("tag", "-l", "v0.1.0").stdout.split()
        self.assertEqual(tags, ["v0.1.0"])
        # 是注解标签（对象类型为 tag，且指向当前 HEAD）
        obj_type = self._bare("cat-file", "-t", "v0.1.0").stdout.strip()
        self.assertEqual(obj_type, "tag")
        target = self._bare("rev-parse", "v0.1.0^{commit}").stdout.strip()
        local_head = _git(["rev-parse", "HEAD"], cwd=repo, env=self.env).stdout.strip()
        self.assertEqual(target, local_head)
        # 注解内容包含演化日志
        tag_body = self._bare("tag", "-l", "--format=%(contents)", "v0.1.0").stdout
        self.assertIn("晋升完成：req-001", tag_body)
        # 无 gh CLI 时给出网页创建提示（本环境无 gh；有 gh 时 release 创建失败也只警告）
        self.assertTrue(
            "gh" in r.stdout or "Release" in r.stdout,
            msg=f"未给出 Release 创建指引：{r.stdout}",
        )

    def test_backup_without_remote_fails_with_diagnosis(self):
        """失败路径：未配置 remote 时返回非 0 且有诊断信息。"""
        repo = self._make_repo(name="repo-noremote", with_remote=False)
        (repo / "app" / "hello.py").write_text(
            '"""有变更"""\nX = 1\n', encoding="utf-8"
        )
        r = self._run_script(repo, "github_backup.sh", "backup: 应失败")
        self.assertNotEqual(r.returncode, 0, msg=r.stdout)
        combined = r.stdout + r.stderr
        self.assertIn("origin", combined)
        self.assertIn("诊断", combined)
        # 提交与标签保留在本地，未污染 bare 仓库
        local_tags = _git(["tag", "-l", "backup/*"], cwd=repo, env=self.env).stdout.split()
        self.assertEqual(len(local_tags), 1)
        bare_refs = self._bare("for-each-ref", "--format=%(refname)").stdout.strip()
        self.assertEqual(bare_refs, "")


if __name__ == "__main__":
    unittest.main()
