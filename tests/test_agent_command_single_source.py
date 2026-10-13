# -*- coding: utf-8 -*-
"""守卫：_agent_command 必须只有一份实现，且配置读不了时要说出真因。

背景（2026-10-14 审计）：``modules/mail_processor._agent_command`` 原先自持一份
只读 settings.json 的实现，与 ``modules.invoice_gui._agent_command`` 是
**同一事实的两个实现**，且：

* 不支持 ``FINANCE_INVOICE_AGENT_COMMAND`` 环境变量覆盖；
* settings.json **损坏时** ``except Exception: pass`` → 返回 None，
  上层于是报「未配置 invoice_agent_command」—— **真因是读不了，不是没配**。
  照这个提示去配命令永远不会好（误导性诊断）。

现已收敛为直接复用 invoice_gui 那一份。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CMD = ["C:/tmp/ocr_venv2/Scripts/python.exe", "C:/Users/li1/invoice-agent/ocr_invoice.py"]


@pytest.fixture()
def cfg_home(tmp_path, monkeypatch):
    """隔离一个数据根，只放 config/settings.json。

    ⚠️ 必须 **reload 被测模块**：``invoice_gui``/``mail_processor`` 的
    ``CONFIG_DIR`` 是**模块级绑定**（``from app_paths import CONFIG_DIR``），
    只 reload ``app_paths`` 不会让它们重新绑定 —— 实测会读到生产配置。
    """
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    monkeypatch.setenv("FINANCE_SYSTEM_HOME", str(home))
    monkeypatch.delenv("FINANCE_INVOICE_AGENT_COMMAND", raising=False)

    import importlib

    import app_paths
    importlib.reload(app_paths)

    from modules import invoice_gui, mail_processor
    importlib.reload(invoice_gui)
    importlib.reload(mail_processor)
    return home, invoice_gui, mail_processor


def _write_settings(home, payload):
    (home / "config" / "settings.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_both_entrypoints_agree_on_configured_command(cfg_home):
    """两份入口（invoice_gui / mail_processor）对同一配置必须给同一结果。"""
    home, invoice_gui, mail_processor = cfg_home
    _write_settings(home, {"invoice_agent_command": CMD})

    assert invoice_gui._agent_command() == CMD
    assert mail_processor._agent_command() == CMD, (
        "mail_processor 与 invoice_gui 对同一配置给出了不同命令 —— "
        "说明又各自实现了一份"
    )


def test_env_var_override_now_honored_by_both(cfg_home):
    """环境变量覆盖对两条链路都生效（原先 mail_processor 不支持）。"""
    home, invoice_gui, mail_processor = cfg_home
    _write_settings(home, {"invoice_agent_command": ["old.exe"]})
    import os

    os.environ["FINANCE_INVOICE_AGENT_COMMAND"] = json.dumps(["new.exe", "a.py"])

    assert invoice_gui._agent_command() == ["new.exe", "a.py"]
    assert mail_processor._agent_command() == ["new.exe", "a.py"], (
        "环境变量覆盖对邮件链路不生效 —— 两份实现又漂移了"
    )


def test_corrupt_settings_logs_real_cause(cfg_home, monkeypatch):
    """🔴 settings.json 损坏时必须记日志说真因，不能静默返回 None。"""
    home, invoice_gui, mail_processor = cfg_home
    (home / "config" / "settings.json").write_text("{ 这不是合法 JSON", encoding="utf-8")

    logged: list = []
    monkeypatch.setattr(
        invoice_gui.LOG, "exception",
        lambda *a, **k: logged.append(a), raising=False,
    )

    result = invoice_gui._agent_command()
    assert result is None
    assert logged, (
        "settings.json 损坏时没有记日志 —— 上层只会说「未配置」，"
        "真因（读不了）被静默吞掉，用户按提示去配命令永远不会好"
    )


def test_source_guard_mail_processor_does_not_reimplement():
    """源码守卫：mail_processor 不许再自持一份配置读取逻辑。"""
    src = (ROOT / "modules" / "mail_processor.py").read_text(encoding="utf-8")
    start = src.find("def _agent_command")
    assert start != -1
    end = src.find("\ndef ", start + 1)
    seg = src[start:end if end != -1 else start + 2000]
    # 剥掉文档字符串再匹配 —— 否则解释"历史原因"的说明文字会误触守卫
    seg = re.sub(r'"""[\s\S]*?"""', '""', seg)
    # 必须是委托，而不是自己读 settings.json
    assert "invoice_gui" in seg, (
        "mail_processor._agent_command 又在自持实现了 —— "
        "请改为复用 modules.invoice_gui._agent_command（单一事实来源）"
    )
    assert "settings.json" not in seg, (
        "mail_processor._agent_command 还在自己读 settings.json"
    )
    # 且不许有静默 pass
    assert "except Exception:\n        pass" not in seg, (
        "mail_processor._agent_command 又出现了静默 except pass"
    )
