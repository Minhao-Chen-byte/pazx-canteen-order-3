#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
磐安中学智慧食堂 - 后台自动订餐守护脚本
可独立运行（无需GUI），支持 Windows 任务计划程序定时执行。

用法:
  python auto_order_daemon.py              # 执行一次订餐检查（默认账号）
  python auto_order_daemon.py --all        # 遍历所有账号各执行一次
  python auto_order_daemon.py --account 1  # 指定账号执行
  python auto_order_daemon.py --loop       # 持续运行（按配置间隔循环）
  python auto_order_daemon.py --notify     # 执行一次并弹出Windows通知
"""

import sys
import os
import time
import json
import argparse
import threading

# 确保能找到主程序模块
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from canting_order import (
    CanteenClient,
    AutoOrderEngine,
    KeywordMatcher,
    MEAL_NAMES,
    load_config,
    AccountsManager,
    ACCOUNTS_FILE,
)

LOG_FILE = os.path.join(SCRIPT_DIR, "auto_order_daemon.log")


def log(msg, also_print=True):
    """写日志到文件"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if also_print:
        print(line)


def send_windows_notification(title, message):
    """发送 Windows 桌面通知"""
    try:
        from plyer import notification

        notification.notify(title=title, message=message, timeout=5)
    except ImportError:
        try:
            # 用 PowerShell 发送通知（无需额外库）
            import subprocess

            ps_script = f'''
            [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
            $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
            $textNodes = $template.GetElementsByTagName("text")
            $textNodes.Item(0).AppendChild($template.CreateTextNode("{title}")) > $null
            $textNodes.Item(1).AppendChild($template.CreateTextNode("{message}")) > $null
            $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
            [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("自动订餐").Show($toast)
            '''
            subprocess.run(
                ["powershell", "-Command", ps_script],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass  # 通知失败不影响主流程


def _load_env_accounts():
    """从环境变量加载多账号列表（GitHub Actions 模式）

    检测 CANTEEN_USERNAME, CANTEEN_USERNAME_2, CANTEEN_USERNAME_3 ...
    返回账号列表，每个账号有 name/username/password/g_id
    """
    accounts = []
    # 账号 1：主环境变量
    u1 = os.environ.get("CANTEEN_USERNAME", "")
    if u1:
        accounts.append({
            "name": os.environ.get("CANTEEN_NAME", "账号1"),
            "username": u1,
            "password": os.environ.get("CANTEEN_PASSWORD", ""),
            "g_id": os.environ.get("CANTEEN_G_ID", ""),
        })
    # 账号 2, 3, ...：索引环境变量
    for i in range(2, 10):
        ui = os.environ.get(f"CANTEEN_USERNAME_{i}", "")
        if not ui:
            break
        accounts.append({
            "name": os.environ.get(f"CANTEEN_NAME_{i}", f"账号{i}"),
            "username": ui,
            "password": os.environ.get(f"CANTEEN_PASSWORD_{i}", ""),
            "g_id": os.environ.get(f"CANTEEN_G_ID_{i}", ""),
        })
    return accounts


def run_once(client=None, account_idx=0):
    """执行一次订餐检查"""
    # GitHub Actions 环境下没有 accounts.json，直接用环境变量凭证
    if os.path.exists(ACCOUNTS_FILE):
        accounts, _ = AccountsManager.load()
        if account_idx < 0 or account_idx >= len(accounts):
            log(f"❌ 账号索引无效: {account_idx} (共{len(accounts)}个)", also_print=True)
            return False
        account = accounts[account_idx]
        AccountsManager.save(accounts, account_idx)
    else:
        # GitHub Actions：凭证从环境变量读取，CONFIG_FILE 用默认值（write_config.py 生成的）
        account = {"name": "GitHub Actions", "username": None, "password": None, "g_id": None}

    config = load_config()
    # 无关键词时用引擎内置的兜底逻辑（选第一个可用套餐）
    has_general_kw = bool(config.get("keywords"))
    has_meal_kw = any(
        config.get(f"{m}_{p}", [])
        for m in ["breakfast", "lunch", "dinner"]
        for p in ["first", "second", "third"]
    )
    if not has_general_kw and not has_meal_kw:
        log(f"ℹ️ [{account['name']}] 未配置关键词，将使用兜底策略（自动选第一个可用套餐）", also_print=True)

    if not client:
        client = CanteenClient(
            username=account["username"],   # None → 回退到 CANTEEN_USERNAME 环境变量
            password=account["password"],   # None → 回退到 CANTEEN_PASSWORD 环境变量
            g_id=account.get("g_id"),       # None → 回退到 CANTEEN_G_ID 环境变量
        )

    # 登录
    success, msg = client.login()
    if not success:
        log(f"❌ [{account['name']}] 登录失败: {msg}")
        return False
    log(f"✅ 登录成功 - {client.user_name} [{account['name']}]")

    # 获取余额
    try:
        info = client.get_user_info()
        balance = float(info.get("balance", 0))
        log(f"💰 当前余额: ¥{balance}")
    except Exception as e:
        log(f"⚠️ 获取余额失败: {e}")

    # 执行自动订餐
    engine = AutoOrderEngine(client)
    log(f"🔍 开始检查可订餐次...")
    log(f"📋 关键词: {config.get('keywords', [])}")
    if config.get("exclude_keywords"):
        log(f"🚫 排除: {config.get('exclude_keywords', [])}")

    results = []
    log_capture = []
    done_event = threading.Event()
    done_result = {}

    def on_log(msg):
        log_capture.append(msg)
        log(msg)

    def on_done(success, msg):
        done_result["success"] = success
        done_result["msg"] = msg
        done_event.set()

    engine.run_once(on_log=on_log, on_done=on_done)

    # 等待完成（最多等 600 秒=10分钟，覆盖~14天×3餐次+网络延迟）
    if not done_event.wait(timeout=600):
        log("⚠️ 订餐超时（600秒），但部分操作可能仍在进行")
    else:
        log(f"✅ 订餐完成信号已收到: {done_result.get('msg', '')}")

    # 检查日志看是否成功
    success_count = sum(1 for m in log_capture if "已订" in m)
    fail_count = sum(1 for m in log_capture if "失败" in m or "异常" in m)

    if success_count > 0:
        log(f"🎉 本次自动订餐完成！成功订了 {success_count} 餐")
        return True
    elif fail_count > 0:
        log(f"⚠️ 完成，但有 {fail_count} 个失败")
        return True
    else:
        log("ℹ️ 没有需要订的新餐次")
        return True


def run_all_once():
    """遍历所有账号，各执行一次订餐检查"""
    # GitHub Actions 模式：从环境变量加载多账号
    if not os.path.exists(ACCOUNTS_FILE):
        env_accounts = _load_env_accounts()
        if len(env_accounts) <= 1:
            log("⚠️ 未找到 accounts.json，且仅检测到 1 个环境变量账号，单账号模式运行")
            return run_once(account_idx=0)

        log(f"\n📋 从环境变量检测到 {len(env_accounts)} 个账号，逐个执行...")
        results = []
        for idx, account in enumerate(env_accounts):
            log(f"\n{'─'*50}")
            log(f"👤 [{idx+1}/{len(env_accounts)}] {account['name']}")
            log(f"{'─'*50}")

            try:
                # 临时切换环境变量，让 CanteenClient 读到当前账号凭证
                old_u = os.environ.get("CANTEEN_USERNAME", "")
                old_p = os.environ.get("CANTEEN_PASSWORD", "")
                old_g = os.environ.get("CANTEEN_G_ID", "")
                os.environ["CANTEEN_USERNAME"] = account["username"]
                os.environ["CANTEEN_PASSWORD"] = account["password"]
                os.environ["CANTEEN_G_ID"] = account["g_id"]

                success = run_once(account_idx=0)

                # 恢复环境变量
                os.environ["CANTEEN_USERNAME"] = old_u
                os.environ["CANTEEN_PASSWORD"] = old_p
                os.environ["CANTEEN_G_ID"] = old_g

                results.append((account["name"], success))
            except Exception as e:
                log(f"❌ [{account['name']}] 异常: {e}")
                results.append((account["name"], False))

            if idx < len(env_accounts) - 1:
                time.sleep(2)

        # 汇总
        log(f"\n{'='*50}")
        log(f"📊 全部账号执行完毕:")
        for name, ok in results:
            status = "✅ 成功" if ok else "❌ 失败"
            log(f"  {status} — {name}")
        log(f"{'='*50}")
        return all(r[1] for r in results)

    # 本地模式：从 accounts.json 加载
    accounts, _ = AccountsManager.load()
    log(f"\n📋 共 {len(accounts)} 个账号，逐个执行...")
    results = []

    for idx, account in enumerate(accounts):
        log(f"\n{'─'*50}")
        log(f"👤 [{idx+1}/{len(accounts)}] {account['name']}")
        log(f"{'─'*50}")

        try:
            success = run_once(account_idx=idx)
            results.append((account["name"], success))
        except Exception as e:
            log(f"❌ [{account['name']}] 异常: {e}")
            results.append((account["name"], False))

        # 账号间短暂间隔，避免请求过快
        if idx < len(accounts) - 1:
            time.sleep(2)

    # 汇总
    log(f"\n{'='*50}")
    log(f"📊 全部账号执行完毕:")
    for name, ok in results:
        status = "✅ 成功" if ok else "❌ 失败"
        log(f"  {status} — {name}")
    log(f"{'='*50}")

    return all(r[1] for r in results)


def run_loop(account_idx=None, all_accounts=False):
    """持续运行模式"""
    if all_accounts:
        accounts, _ = AccountsManager.load()
        names = ", ".join(a["name"] for a in accounts)
        log(f"🔄 [{names}] 启动持续监控")
    else:
        config = load_config()
        interval = config.get("interval_minutes", 30)
        accounts, _ = AccountsManager.load()
        idx = account_idx if account_idx is not None else 0
        name = accounts[idx]["name"] if idx < len(accounts) else "?"
        log(f"🔄 [{name}] 启动持续监控，每 {interval} 分钟检查一次")

    cycle = 0
    while True:
        cycle += 1
        config = load_config()
        interval = config.get("interval_minutes", 30)

        log(f"\n{'='*50}")
        log(f"📌 第 {cycle} 次检查 ({time.strftime('%Y-%m-%d %H:%M:%S')})")

        try:
            if all_accounts:
                run_all_once()
            else:
                run_once(None, account_idx if account_idx is not None else 0)
        except Exception as e:
            log(f"❌ 检查异常: {e}")

        log(f"⏳ 等待 {interval} 分钟后下一次检查...")
        log(f"{'='*50}\n")

        time.sleep(interval * 60)


def main():
    parser = argparse.ArgumentParser(
        description="磐安中学智慧食堂 - 后台自动订餐"
    )
    parser.add_argument(
        "--account", "-a",
        type=int, default=None,
        help="账号索引 (0=第一个, 1=第二个, 默认0)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="遍历所有账号各执行一次",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="持续运行模式（按配置间隔循环检查）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="执行一次后退出（默认）",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="执行一次并发送 Windows 通知",
    )

    args = parser.parse_args()

    all_accounts = args.all
    account_idx = args.account if args.account is not None else 0

    # 显示可用账号
    accounts, _ = AccountsManager.load()
    names = ", ".join(f"[{i}] {a['name']}" for i, a in enumerate(accounts))
    log("=" * 50)
    log(f"🍽️  磐安中学智慧食堂 - 自动订餐")
    log(f"📋 可用账号: {names}")
    if all_accounts:
        log(f"🎯 模式: 全账号遍历")
    log("=" * 50)

    if args.loop:
        run_loop(account_idx, all_accounts=all_accounts)
    else:
        success = run_all_once() if all_accounts else run_once(account_idx=account_idx)
        if args.notify:
            send_windows_notification(
                "自动订餐结果",
                "全部完成！" if success else "订餐失败，请查看日志",
            )


if __name__ == "__main__":
    main()
