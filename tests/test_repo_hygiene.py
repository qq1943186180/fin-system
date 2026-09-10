# -*- coding: utf-8 -*-
"""仓库卫生回归测试：密钥类文件必须被 .gitignore 挡住，示例文件必须放行。

背景（2026-09-10 修复）：

原 `.gitignore` 只逐条忽略了 `config/feishu.json`，而
`config/credentials.json`（**全部密钥的仓库**：邮箱授权码 + 飞书 secret 的加密 blob）、
`config/settings.json`、`config/*.blob` **都不被忽略**（`git check-ignore` 实测）。
当前这些文件都在 `%LOCALAPPDATA%\\FinanceSystem\\config`（安全），
但项目根**已存在 `config/` 目录且含 `feishu.json`** —— 说明「往项目根放配置」
这个动作真实发生过。一旦调试时把 `credentials.json` 拷进来再 `git add .`，
密钥仓库就进版本历史了。

修复方式：把逐条列举改为按类型覆盖（`config/*.json` + `config/*.blob`），
并用 `!config/feishu.example.json` 放行示例文件。这样**以后新增的配置文件默认安全**，
不需要每次记得往 .gitignore 里加一行。

注意：规则写的是 `config/*.json` 而不是 `config/`—— 若忽略整个目录，
`!` 放行规则会被父目录规则压掉，示例文件就救不回来了。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# git 不可用时跳过（例如源码被拷到非仓库目录跑测试）
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="需要 git 才能验证 .gitignore 行为"
)


def _is_ignored(relative_path: str) -> bool:
    """用 git 自己的匹配引擎判断，而不是在 Python 里手写 glob 模拟。"""
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", relative_path],
        cwd=PROJECT_ROOT,
        capture_output=True,
    )
    if result.returncode == 128:
        pytest.skip("当前目录不是 git 仓库")
    return result.returncode == 0


@pytest.mark.parametrize(
    "relative_path",
    [
        "config/credentials.json",        # 全部密钥的仓库（授权码 + secret blob）
        "config/settings.json",           # 邮箱地址 / OCR 命令路径
        "config/feishu.json",             # 历史遗留位置
        "config/feishu.app_secret.blob",  # 旧版残留 blob
        "config/anything-new.json",       # 未来的新配置文件应默认安全
        "config/whatever.blob",
    ],
)
def test_secret_like_config_files_are_ignored(relative_path: str) -> None:
    assert _is_ignored(relative_path), "%s 必须被 .gitignore 挡住" % relative_path


@pytest.mark.parametrize(
    "relative_path",
    [
        "config/feishu.example.json",  # 示例文件必须能进仓库（.gitignore 里 ! 放行）
        "config/README.md",
    ],
)
def test_example_and_docs_are_not_ignored(relative_path: str) -> None:
    assert not _is_ignored(relative_path), "%s 不应被忽略" % relative_path


def test_no_secret_file_is_tracked_by_git() -> None:
    """纵深防御第二层：即使 ignore 规则被改坏，也不应有密钥文件已被跟踪。

    已跟踪文件不受 .gitignore 约束 —— 一旦进去就必须 `git rm --cached` 才能出来，
    且内容已留在历史里。这条测试是「已经出事」的报警器。
    """
    result = subprocess.run(
        ["git", "ls-files", "config/"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("无法列出 git 跟踪文件（可能不是仓库）")

    tracked = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    offenders = [
        name
        for name in tracked
        if name.endswith(".blob")
        or name.endswith("credentials.json")
        or name.endswith("settings.json")
        or (name.endswith(".json") and "example" not in name)
    ]
    assert not offenders, "以下敏感文件已被 git 跟踪，必须 `git rm --cached` 并轮换密钥：%s" % offenders


def test_gitignore_does_not_ignore_config_directory_itself() -> None:
    """忽略整个 `config/` 目录会让 `!` 放行规则失效 —— 锁住这个写法。"""
    text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    offending = [
        line.strip()
        for line in text.splitlines()
        if line.strip() in ("config/", "/config/", "config")
    ]
    assert not offending, (
        "`.gitignore` 不应忽略 config 目录本身（否则 !config/feishu.example.json 失效）：%s"
        % offending
    )
