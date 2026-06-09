#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
磐安中学智慧食堂 - 订餐程序
支持浏览菜单、加入购物车、下单、查看订单等功能
"""

import re
import json
import os
import http.cookiejar
import urllib.request
import urllib.parse
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from datetime import datetime, timedelta
from Crypto.Cipher import DES, AES
from Crypto.Util.Padding import pad
from Crypto.Random import get_random_bytes
import base64

# 可选 AI 依赖
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False
try:
    import openai
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

# ==================== 配置 ====================
BASE_URL = "http://pazx.yksmart3.com:18288"
G_ID = os.environ.get("CANTEEN_G_ID", "")
USERNAME = os.environ.get("CANTEEN_USERNAME", "")
PASSWORD = os.environ.get("CANTEEN_PASSWORD", "")
BIZ_ID = os.environ.get("CANTEEN_BIZ_ID", "81011")
ORG_ID = os.environ.get("CANTEEN_ORG_ID", "01")
SUB_ORG_ID = os.environ.get("CANTEEN_SUB_ORG_ID", "000")

MEAL_NAMES = {1: "早餐", 2: "午餐", 3: "晚餐"}
MEAL_COLORS = {1: "#FF8C00", 2: "#2E8B57", 3: "#4169E1"}

ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), "accounts.json")
MASTER_KEY_FILE = os.path.join(os.path.dirname(__file__), ".master_key")
ENCRYPTED_PREFIX = "AES$"


def _get_or_create_key():
    """获取或创建本地主密钥（256-bit AES），密钥文件不提交 Git"""
    if os.path.exists(MASTER_KEY_FILE):
        with open(MASTER_KEY_FILE, "rb") as f:
            return f.read()
    key = get_random_bytes(32)
    with open(MASTER_KEY_FILE, "wb") as f:
        f.write(key)
    # Windows 下隐藏密钥文件
    try:
        import subprocess
        subprocess.run(["attrib", "+H", MASTER_KEY_FILE],
                       capture_output=True, shell=True)
    except Exception:
        pass
    return key


def _encrypt_field(plaintext):
    """AES-256-GCM 加密字符串，返回带前缀的 base64 密文（已加密的值不再重复加密）"""
    if not plaintext or plaintext.startswith(ENCRYPTED_PREFIX):
        return plaintext
    key = _get_or_create_key()
    cipher = AES.new(key, AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    return ENCRYPTED_PREFIX + base64.b64encode(
        cipher.nonce + tag + ciphertext
    ).decode()


def _decrypt_field(encoded):
    """解密 _encrypt_field 产生的密文，非加密值原样返回"""
    if not encoded or not encoded.startswith(ENCRYPTED_PREFIX):
        return encoded
    try:
        key = _get_or_create_key()
        raw = base64.b64decode(encoded[len(ENCRYPTED_PREFIX):])
        nonce, tag, ciphertext = raw[:16], raw[16:32], raw[32:]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
    except Exception:
        return encoded  # 解密失败则返回原值，避免数据丢失


# ==================== 多账号管理 ====================
class AccountsManager:
    """管理多个食堂账号，每个账号有独立的凭证和订餐配置"""

    DEFAULT_ACCOUNTS = [
        {
            "name": "新账号",
            "username": "",
            "password": "",
            "g_id": "",
            "config_file": "auto_order_config.json",
            "github_repo": "",
        },
    ]

    @classmethod
    def load(cls):
        """加载账号列表（自动解密敏感字段）"""
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            accounts = data.get("accounts", [])
            active = data.get("active", 0)
            if not accounts:
                accounts = cls.DEFAULT_ACCOUNTS
                active = 0
            # 解密敏感字段（__encrypted__ 为 True 时解密，否则按需尝试）
            is_encrypted = data.get("__encrypted__", False)
            for acc in accounts:
                for field in ("username", "password", "g_id"):
                    val = acc.get(field, "")
                    if val:
                        if is_encrypted or val.startswith(ENCRYPTED_PREFIX):
                            acc[field] = _decrypt_field(val)
        except (FileNotFoundError, json.JSONDecodeError):
            accounts = cls.DEFAULT_ACCOUNTS
            active = 0
        cls._sync_config_file(accounts, active)
        return accounts, active

    @classmethod
    def _sync_config_file(cls, accounts, active):
        """将全局 CONFIG_FILE 指向激活账号的配置文件"""
        global CONFIG_FILE
        if 0 <= active < len(accounts):
            cfg_file = accounts[active].get("config_file", "auto_order_config.json")
            CONFIG_FILE = os.path.join(os.path.dirname(__file__), cfg_file)

    @classmethod
    def save(cls, accounts, active=0):
        """保存账号列表（自动加密敏感字段）"""
        encrypted_accounts = []
        for acc in accounts:
            ea = dict(acc)
            for field in ("username", "password", "g_id"):
                if field in ea and ea[field]:
                    ea[field] = _encrypt_field(ea[field])
            encrypted_accounts.append(ea)
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump({"accounts": encrypted_accounts, "active": active,
                       "__encrypted__": True}, f, ensure_ascii=False, indent=2)
        cls._sync_config_file(accounts, active)

    @classmethod
    def get_active(cls):
        """返回当前激活账号"""
        accounts, active = cls.load()
        if 0 <= active < len(accounts):
            return accounts[active]
        return accounts[0] if accounts else cls.DEFAULT_ACCOUNTS[0]

    @classmethod
    def get_config_file(cls, account=None):
        """返回账号的配置文件路径"""
        if account is None:
            account = cls.get_active()
        cfg_file = account.get("config_file", "auto_order_config.json")
        return os.path.join(os.path.dirname(__file__), cfg_file)

    @classmethod
    def generate_config_file(cls, accounts):
        """为新账号生成唯一的配置文件名"""
        existing = {a.get("config_file", "") for a in accounts}
        idx = 1
        while True:
            name = f"auto_order_config_{idx}.json" if idx > 1 else "auto_order_config.json"
            if name not in existing:
                return name
            idx += 1


# ==================== API 客户端 ====================
class CanteenClient:
    def __init__(self, username=None, password=None, g_id=None):
        # 账号凭证：参数 > 环境变量 > 模块全局默认
        self._username = username or os.environ.get("CANTEEN_USERNAME", USERNAME)
        self._password = password or os.environ.get("CANTEEN_PASSWORD", PASSWORD)
        self._g_id = g_id or os.environ.get("CANTEEN_G_ID", G_ID)

        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj)
        )
        self.opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36"),
        ]
        self.logged_in = False
        self.user_name = ""
        self.balance = ""

    def _encrypt_password(self, password, des_key):
        """DES/ECB/PKCS7 加密密码"""
        key_bytes = des_key[:8].encode("utf-8")
        cipher = DES.new(key_bytes, DES.MODE_ECB)
        encrypted = cipher.encrypt(pad(password.encode("utf-8"), 8))
        return base64.b64encode(encrypted).decode()

    def _extract_des_key(self, html):
        """从登录页面提取动态 DES 密钥"""
        match = re.search(
            r"encryptByDES\(mui\('\#psw'\)\[0\].value,\s*'([^']+)'\)", html
        )
        if match:
            return match.group(1)
        raise Exception("无法提取 DES 密钥")

    def login(self):
        """登录食堂系统"""
        url = f"{BASE_URL}/weixin/getWxLoginPage.action?_g_id={self._g_id}"
        resp = self.opener.open(url)
        html = resp.read().decode("utf-8")

        des_key = self._extract_des_key(html)
        enc_pwd = self._encrypt_password(self._password, des_key)

        login_url = f"{BASE_URL}/weixin/loginWxCatering.action?_g_id={self._g_id}"
        data = urllib.parse.urlencode({
            "idcard": self._username,
            "psw": enc_pwd,
            "wxid": "",
            "serivceo": "",
        }).encode("utf-8")

        resp = self.opener.open(login_url, data=data)
        html = resp.read().decode("utf-8")

        # 检查是否登录成功（被重定向到首页）
        if "getPage_getInfoPage.action" in html or "订餐" in html or "磐安中学" in html:
            self.logged_in = True
            # 提取用户名
            name_match = re.search(r'value="([^"]*)"\s*name="name"', html)
            user_match = re.search(r"陈茗浩", html)
            if user_match:
                self.user_name = "陈茗浩"
            return True, "登录成功！"
        else:
            err_match = re.search(r"<h4[^>]*>([^<]*)</h4>", html)
            err = err_match.group(1).strip() if err_match else "登录失败"
            return False, err

    def _get(self, url):
        """GET 请求"""
        resp = self.opener.open(url)
        return resp.read().decode("utf-8")

    def _post(self, url, data_dict):
        """POST 请求"""
        data = urllib.parse.urlencode(data_dict).encode("utf-8")
        resp = self.opener.open(url, data=data)
        return resp.read().decode("utf-8")

    def _post_json(self, url, data_dict):
        """POST 请求并解析 JSON"""
        res = self._post(url, data_dict)
        return json.loads(res)

    def get_menu(self, date_str, meal_code):
        """获取指定日期和餐次的菜单"""
        url = f"{BASE_URL}/weixin/getPage_getShopCc.action?datecs={date_str}&cc={meal_code}&bizid={BIZ_ID}&floor=0"
        html = self._get(url)
        return self._parse_menu_items(html)

    def _parse_menu_items(self, html):
        """从 HTML 解析菜单项 - 按 li 块逐个解析"""
        items = []
        # 先把每个菜单项按 li 块分割
        blocks = re.findall(
            r'<li class="mui-table-view-cell1 mui-media">(.*?)</li>',
            html,
            re.DOTALL,
        )
        for block in blocks:
            # 提取套餐名称 (在 ylyTitle 中)
            name_match = re.search(
                r'<div class="ylyTitle">(.*?)</div>', block, re.DOTALL
            )
            if not name_match:
                continue
            name_html = name_match.group(1)
            # 提取 mealTime 标签
            meal_time_m = re.search(
                r'<span class="mealTime"[^>]*>([^<]*)</span>', name_html
            )
            meal_time = meal_time_m.group(1).strip() if meal_time_m else ""
            # 提取套餐名（去掉 HTML 标签后取最后非空行）
            name_text = re.sub(r"<[^>]+>", "", name_html).strip()
            name_lines = [l.strip() for l in name_text.split("\n") if l.strip()]
            name = name_lines[-1] if name_lines else name_text

            # 提取描述
            desc_match = re.search(
                r"<p class='mui-ellipsis product'>(.*?)</p>", block, re.DOTALL
            )
            desc = (
                re.sub(r"<[^>]+>", "", desc_match.group(1)).strip()
                if desc_match
                else ""
            )

            # 提取价格
            price_match = re.search(
                r'<span class="price">¥([\d.]+)</span>', block
            )
            if not price_match:
                continue
            price = float(price_match.group(1))

            # 提取已售/总量
            sold_match = re.search(r"已售(\d+)/(\d+)", block)
            sold = int(sold_match.group(1)) if sold_match else 0
            total = int(sold_match.group(2)) if sold_match else 0

            # 提取 setmno (套餐编号)
            setmno_match = re.search(r"jlgwc3\('(\d+)'", block)
            if not setmno_match:
                continue
            setmno = setmno_match.group(1)

            items.append({
                "setmno": setmno,
                "name": name,
                "meal_time": meal_time,
                "description": desc,
                "price": price,
                "sold": sold,
                "total": total,
                "available": total - sold,
            })
        return items

    def visit_shop_page(self, date_str, meal_code=1):
        """访问订餐页面（初始化/切换餐次 session 状态）"""
        url = f"{BASE_URL}/weixin/getPage_getShopCc.action?datecs={date_str}&cc={meal_code}&bizid={BIZ_ID}&floor=0"
        try:
            self._get(url)
            return True
        except Exception:
            return False

    def add_to_cart(self, setmno, date_str, meal_code):
        """添加到购物车"""
        url = f"{BASE_URL}/weixin/jrgwc4wx.action"
        result = self._post_json(url, {
            "setmno": setmno,
            "ydsl": "1",
            "orgid": ORG_ID,
            "suborgid": SUB_ORG_ID,
            "bizid": BIZ_ID,
            "datecs": date_str,
            "cc": str(meal_code),
        })
        return result

    def get_cart(self):
        """获取购物车内容"""
        url = f"{BASE_URL}/weixin/getPage_getCartPage.action"
        html = self._get(url)
        items = []
        # 解析购物车中的套餐项
        pattern = (
            r'id="(\d+)(\d{4}-\d{2}-\d{2})(\d)num".*?'
            r'<span[^>]*>(\d+)</span>.*?'
            r'div class="ylyTitle">[^<]*<[^>]*>[^<]*</[^>]*>\s*([^<]+)'
        )
        # 更简单的解析方式：提取套餐编号
        pattern2 = r'id="(\d+)(\d{4}-\d{2}-\d{2})(\d)num".*?>\s*(\d+)\s*<'
        matches = re.findall(pattern2, html, re.DOTALL)
        for setmno, date_str, cc, qty in matches:
            items.append({
                "setmno": setmno,
                "date_str": date_str,
                "meal_code": int(cc),
                "quantity": int(qty),
            })

        # 提取总价
        total_match = re.search(r'id="totalPrice"[^>]*>¥?([\d.]+)', html)
        total = float(total_match.group(1)) if total_match else 0.0

        # 提取数量
        count_match = re.search(r'id="countspan"[^>]*>\((\d+)\)', html)
        count = int(count_match.group(1)) if count_match else 0

        return items, total, count, html

    def delete_from_cart(self, setmno, date_str, meal_code):
        """从购物车删除"""
        url = f"{BASE_URL}/weixin/deleteOrderNum4wx.action"
        result = self._post_json(url, {
            "setmno": setmno,
            "ydsl": "1",
            "orgid": ORG_ID,
            "suborgid": SUB_ORG_ID,
            "bizid": BIZ_ID,
            "dinnerdate": date_str,
            "commit": "0",
        })
        return result

    def cancel_order(self, setmno, date_str, meal_code, committed=True):
        """取消指定套餐的订单（commit=1 取消已订，commit=0 取消购物车）"""
        url = f"{BASE_URL}/weixin/deleteOrderNum4wx.action"
        result = self._post_json(url, {
            "setmno": setmno,
            "ydsl": "1",
            "orgid": ORG_ID,
            "suborgid": SUB_ORG_ID,
            "bizid": BIZ_ID,
            "dinnerdate": date_str,
            "commit": "1" if committed else "0",
        })
        return result

    def cancel_all_orders(self, on_log=None):
        """一键取消所有已订餐"""
        log_func = on_log or (lambda _: None)
        log_func("🔍 获取订餐记录...")
        orders = self.get_order_records()

        if not orders:
            log_func("📭 没有已订的餐")
            return [], []

        log_func(f"📋 共 {len(orders)} 条订餐记录")
        cancelled = []
        failed = []

        for order in orders:
            date_str = order["date"]
            meal_code = order["meal_code"]
            name = order["name"]

            # 标准化日期
            date_clean = date_str.replace("/", "-")
            parts = date_clean.split("-")
            if len(parts) == 3:
                full_date = date_clean
            elif len(parts) == 2:
                full_date = f"{datetime.now().year}-{int(parts[0]):02d}-{int(parts[1]):02d}"
            else:
                failed.append(f"{date_str} {name}")
                continue

            meal_name = MEAL_NAMES.get(meal_code, "?")
            log_func(f"📋 {full_date} {meal_name}: {name}")

            # 未知餐次 → 遍历所有
            if meal_code == 0:
                for mc in range(1, 4):
                    ok, msg = self._cancel_single(full_date, mc, name, log_func)
                    if ok:
                        cancelled.append(msg)
                        break
                else:
                    failed.append(f"{full_date} {name}")
                continue

            ok, msg = self._cancel_single(full_date, meal_code, name, log_func)
            if ok:
                cancelled.append(msg)
            else:
                failed.append(f"{full_date} {meal_name} {name}")

            time.sleep(0.3)

        log_func(f"📊 取消完成: {len(cancelled)} 成功 / {len(failed)} 失败")
        return cancelled, failed

    def _cancel_single(self, full_date, meal_code, item_name, log_func):
        """对单个日期×餐次执行取消，返回 (成功?, 日志消息)"""
        meal_name = MEAL_NAMES.get(meal_code, "?")

        self.visit_shop_page(full_date, meal_code)
        time.sleep(0.3)
        items = self.get_menu(full_date, meal_code)

        if not items:
            return False, ""

        # 按名称匹配
        candidates = [it for it in items if item_name in it["name"] or it["name"] in item_name]
        if not candidates:
            candidates = items

        # 策略1: commit=1（取消已提交订单）
        for item in candidates:
            try:
                r = self.cancel_order(item["setmno"], full_date, meal_code, committed=True)
                if r.get("flag"):
                    log_func(f"  ✅ 已取消: {item['name']}")
                    return True, f"{full_date} {meal_name} {item['name']}"
            except Exception:
                continue

        # 策略2: commit=0（购物车模式）
        for item in candidates:
            try:
                r = self.cancel_order(item["setmno"], full_date, meal_code, committed=False)
                if r.get("flag"):
                    log_func(f"  ✅ 已取消: {item['name']}")
                    return True, f"{full_date} {meal_name} {item['name']}"
            except Exception:
                continue

        return False, ""

    def submit_order(self):
        """提交订单（主账户支付）"""
        url = f"{BASE_URL}/weixin/tjdj4wx.action"
        result = self._post_json(url, {
            "orgid": ORG_ID,
            "address": "",
            "payway": "7",
        })
        return result

    def get_user_info(self):
        """获取用户信息"""
        url = f"{BASE_URL}/weixin/getPage_getUserPage.action"
        html = self._get(url)

        name_match = re.search(r'<span class="userName">([^<]+)</span>', html)
        balance_match = re.search(
            r'<span class="moneySpan">([\d.]+)</span>\s*<span class="textSpan">账号余额',
            html,
        )

        info = {}
        if name_match:
            info["name"] = name_match.group(1)
        if balance_match:
            info["balance"] = balance_match.group(1)

        return info

    def get_order_records(self):
        """获取订餐记录"""
        url = f"{BASE_URL}/weixin/queryUserOrder.action"
        html = self._get(url)
        orders = []
        MEAL_MAP = {"早餐": 1, "午餐": 2, "晚餐": 3}

        # 按日期分组解析
        date_sections = re.findall(
            r'<div class="menuTime">([^<]+)</div>(.*?)'
            r"(?=<div class=\"menuTime\">|\s*$|</div>\s*$)",
            html,
            re.DOTALL,
        )

        for date_str, section in date_sections:
            items = re.findall(
                r'<div class="ylyTitle">[^<]*(?:\s*<[^>]*>([^<]*)</[^>]*>\s*)?([^<]+)</div>.*?'
                r"<span class=\"price\">¥(\d+(?:\.\d+)?)",
                section,
                re.DOTALL,
            )
            for meal_time_text, name, price in items:
                meal_code = MEAL_MAP.get(meal_time_text.strip(), 0)
                orders.append({
                    "date": date_str.strip(),
                    "meal_code": meal_code,
                    "name": name.strip(),
                    "price": float(price),
                })

        # 如果正则没匹配到，用简单方法
        if not orders:
            dates = re.findall(r'<div class="menuTime">([^<]+)</div>', html)
            names = re.findall(
                r'<div class="ylyTitle">[^<]*(?:\s*<[^>]*>[^<]*</[^>]*>\s*)?([^<]+?)</div>',
                html,
            )
            prices = re.findall(r'<span class="price">¥([\d.]+)</span>', html)
            for i, d in enumerate(dates):
                n = names[i] if i < len(names) else "未知"
                p = float(prices[i]) if i < len(prices) else 0
                orders.append({"date": d, "meal_code": 0, "name": n.strip(), "price": p})

        return orders

    def get_settlement_summary(self):
        """获取结算汇总"""
        url = f"{BASE_URL}/weixin/getPage_getSettlementPage.action?toDinnerRpt=1"
        html = self._get(url)
        summary = []
        # 解析订餐统计表
        rows = re.findall(
            r"<td>([^<]+)<br>([^<]+)</td>(.*?)</tr>", html, re.DOTALL
        )
        for weekday, date_str, cols_html in rows:
            meals = re.findall(
                r"<td>(.*?)</td>", cols_html, re.DOTALL
            )
            row = {"weekday": weekday.strip(), "date": date_str.strip(), "meals": []}
            for m in meals:
                status = "未订"
                if "未订" not in m:
                    status_match = re.search(r"(\d+)\s*份", m)
                    if status_match:
                        status = f"已订{status_match.group(1)}份"
                row["meals"].append(status)
            summary.append(row)

        # 提取总计
        total_match = re.search(r'¥([\d.]+)', html)
        total = float(total_match.group(1)) if total_match else 0.0
        return summary, total


# ==================== 自动订餐配置 ====================
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "auto_order_config.json")

DEFAULT_CONFIG = {
    "enabled": False,
    "keywords": ["排骨", "牛肉", "猪蹄", "鸡"],
    "exclude_keywords": [],
    "breakfast_keywords": [],
    "lunch_keywords": [],
    "dinner_keywords": [],
    "breakfast_first": [],
    "breakfast_second": [],
    "breakfast_third": [],
    "lunch_first": [],
    "lunch_second": [],
    "lunch_third": [],
    "dinner_first": [],
    "dinner_second": [],
    "dinner_third": [],
    "interval_minutes": 30,
    "portions_per_meal": 1,
    "max_meals_per_day": 6,
    "min_balance": 5.0,
    "ai_enabled": False,
    "ai_api_type": "openai",
    "ai_api_key": "",
    "ai_model": "deepseek-v4-flash",
    "ai_api_url": "https://api.deepseek.com/v1",
}


def load_config():
    """加载配置文件（自动解密 ai_api_key）"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
            # 合并默认值，确保新字段存在
            for k, v in DEFAULT_CONFIG.items():
                config.setdefault(k, v)
            # 解密 API Key
            if config.get("ai_api_key"):
                config["ai_api_key"] = _decrypt_field(config["ai_api_key"])
            return config
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)


def save_config(config):
    """保存配置文件（自动加密 ai_api_key）"""
    cfg = dict(config)
    if cfg.get("ai_api_key"):
        cfg["ai_api_key"] = _encrypt_field(cfg["ai_api_key"])
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ==================== 关键词匹配引擎 ====================
class KeywordMatcher:
    """根据用户关键词对套餐进行打分排序"""

    @staticmethod
    def score(items, keywords, exclude_keywords):
        """对菜单项进行关键词匹配打分，返回按得分降序排列的列表"""
        if not keywords:
            return list(items)

        scored = []
        for item in items:
            score_val = 0
            text = (item["name"] + " " + item["description"]).lower()

            # 正向匹配
            for kw in keywords:
                if kw.lower() in text:
                    score_val += 1

            # 排除关键词（一票否决减分）
            for ekw in exclude_keywords:
                if ekw.lower() in text:
                    score_val -= 10

            scored.append((score_val, item))

        # 按得分降序，同分按价格升序
        scored.sort(key=lambda x: (-x[0], x[1]["price"]))
        return [s[1] for s in scored]


# ==================== AI 推荐增强 ====================
class AIEnhancer:
    """AI 推荐增强模块 — 支持 DeepSeek (OpenAI 兼容) 和 Anthropic 双后端"""

    BACKEND_OPENAI = "openai"
    BACKEND_ANTHROPIC = "anthropic"

    def __init__(self, api_key="", model="deepseek-v4-flash",
                 api_url="https://api.deepseek.com/v1", backend=BACKEND_OPENAI):
        self.api_key = api_key
        self.model = model
        self.api_url = api_url.rstrip("/")
        self.backend = backend
        self._http_headers = {}

    def is_available(self):
        """检查 AI 是否可用"""
        if not self.api_key:
            return False
        if self.backend == self.BACKEND_ANTHROPIC and not HAS_ANTHROPIC:
            return False
        if self.backend == self.BACKEND_OPENAI and not HAS_OPENAI:
            # 即使没有 openai 库，也可以直接用 urllib 做 HTTP 请求
            pass
        return True

    def rerank(self, items, keywords, history):
        """用 AI 重排序套餐列表，失败时返回原文"""
        if not self.is_available() or not items:
            return items

        menu_text = "\n".join(
            f"{i+1}. {it['name']} - {it['description']} - ¥{it['price']}"
            for i, it in enumerate(items)
        )
        history_text = "\n".join(history[-10:]) if history else "暂无历史"

        prompt = (
            "你是学校食堂的智能营养师。请根据用户口味和营养搭配，"
            "从以下套餐中选出最推荐的一个。只需返回一个数字（套餐编号）。\n\n"
            f"用户口味关键词: {', '.join(keywords)}\n"
            f"近期已订餐:\n{history_text}\n\n"
            f"可选套餐:\n{menu_text}\n\n"
            "请返回最推荐的套餐编号 (如: 1)，仅返回数字，不要其他文字。"
        )

        try:
            if self.backend == self.BACKEND_ANTHROPIC and HAS_ANTHROPIC:
                choice_text = self._call_anthropic(prompt)
            else:
                choice_text = self._call_openai(prompt)

            nums = re.findall(r"\d+", choice_text.strip())
            if nums:
                idx = int(nums[0]) - 1
                if 0 <= idx < len(items):
                    result = [items[idx]]
                    result.extend(it for i, it in enumerate(items) if i != idx)
                    return result
            return items
        except Exception:
            return items  # 降级：返回原文

    def _call_openai(self, prompt):
        """调用 OpenAI 兼容 API (DeepSeek)"""
        req_body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 10,
            "temperature": 0.3,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self.api_url}/chat/completions",
            data=req_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    def _call_anthropic(self, prompt):
        """调用 Anthropic API (Claude)"""
        req_body = json.dumps({
            "model": self.model,
            "max_tokens": 10,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self.api_url}/messages",
            data=req_body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))
        return data["content"][0]["text"]


# ==================== 操作日志 ====================
class OrderLogger:
    """自动订餐操作日志"""

    def __init__(self):
        self.logs = []

    def add(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        icons = {"SUCCESS": "✅", "INFO": "ℹ️", "ERROR": "❌", "WARN": "⚠️", "AI": "🤖"}
        icon = icons.get(level, "📋")
        entry = f"[{timestamp}] {icon} {message}"
        self.logs.append(entry)
        return entry

    def get_all(self):
        return list(self.logs)

    def get_recent(self, n=50):
        return self.logs[-n:]


# ==================== 自动订餐引擎 ====================
class AutoOrderEngine:
    """自动订餐引擎 —— 定时检测菜单更新并自动下单"""

    def __init__(self, client):
        self.client = client
        self.config = load_config()
        self.matcher = KeywordMatcher()
        self.ai = AIEnhancer(
            api_key=self.config.get("ai_api_key", ""),
            model=self.config.get("ai_model", "deepseek-v4-flash"),
            api_url=self.config.get("ai_api_url", "https://api.deepseek.com/v1"),
            backend=self.config.get("ai_api_type", AIEnhancer.BACKEND_OPENAI),
        )
        self.logger = OrderLogger()
        self._timer = None
        self._running = False
        self._paused = False
        self._ordered_set = set()  # 已订过的 (date, meal_code) 集合

    # ---- 配置 ----

    def update_config(self, **kwargs):
        """更新配置并保存"""
        self.config.update(kwargs)
        save_config(self.config)
        # 同步 AI 配置
        self.ai.api_key = self.config.get("ai_api_key", "")
        self.ai.model = self.config.get("ai_model", "deepseek-v4-flash")
        self.ai.api_url = self.config.get("ai_api_url", "https://api.deepseek.com/v1")
        self.ai.backend = self.config.get("ai_api_type", AIEnhancer.BACKEND_OPENAI)

    def get_config(self):
        return dict(self.config)

    # ---- 控制 ----

    def start(self, on_log=None):
        """启动自动订餐"""
        if self._running:
            return
        self._running = True
        self._paused = False
        self.logger.add("自动订餐已启动", "INFO")
        if on_log:
            on_log(self.logger.get_recent(1)[0])
        self._schedule_check(on_log)

    def stop(self, on_log=None):
        """停止自动订餐"""
        self._running = False
        self._paused = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self.logger.add("自动订餐已停止", "INFO")
        if on_log:
            on_log(self.logger.get_recent(1)[0])

    def pause(self, on_log=None):
        """暂停"""
        self._paused = True
        self.logger.add("自动订餐已暂停", "INFO")
        if on_log:
            on_log(self.logger.get_recent(1)[0])

    def resume(self, on_log=None):
        """恢复"""
        self._paused = False
        self.logger.add("自动订餐已恢复", "INFO")
        if on_log:
            on_log(self.logger.get_recent(1)[0])

    def run_once(self, on_log=None, on_done=None):
        """立即执行一次订餐检查（异步）"""
        threading.Thread(
            target=self._do_check, args=(on_log, on_done), daemon=True
        ).start()

    # ---- 定时器 ----

    def _schedule_check(self, on_log=None):
        if not self._running:
            return
        interval = self.config.get("interval_minutes", 30) * 60
        self._timer = threading.Timer(interval, self._tick, args=[on_log])
        self._timer.daemon = True
        self._timer.start()

    def _tick(self, on_log=None):
        if self._paused or not self._running:
            self._schedule_check(on_log)
            return
        self._do_check(on_log)
        self._schedule_check(on_log)

    # ---- 核心检查逻辑 ----

    def _do_check(self, on_log=None, on_done=None):
        """检查并订餐"""
        try:
            # 0. 重新加载配置（确保读到最新设置）
            self.config = load_config()

            # 1. 检查登录状态
            if not self.client.logged_in:
                success, _ = self.client.login()
                if not success:
                    msg = self.logger.add("登录失败，跳过本次检查", "ERROR")
                    if on_log:
                        on_log(msg)
                    if on_done:
                        on_done(False, msg)
                    return

            # 2. 获取已订餐信息
            summary, total = self.client.get_settlement_summary()
            if not summary:
                msg = self.logger.add("获取订餐统计失败", "ERROR")
                if on_log:
                    on_log(msg)
                if on_done:
                    on_done(False, msg)
                return

            # 3. 找出未订的餐次
            keywords = self.config.get("keywords", [])
            exclude_kw = self.config.get("exclude_keywords", [])
            ai_enabled = self.config.get("ai_enabled", False)
            max_meals = self.config.get("max_meals_per_day", 6)
            min_balance = self.config.get("min_balance", 5.0)
            ordered_count = 0

            for row_idx, row in enumerate(summary):
                # 已达总上限，跳过剩余日期
                if ordered_count >= max_meals:
                    msg = self.logger.add(f"📌 已达订餐总数上限({max_meals}餐)，停止检查", "INFO")
                    if on_log: on_log(msg)
                    break

                date_str = row["date"]
                # 只跳过昨天及更早的日期（保留今天，允许当日订餐）
                try:
                    dt = datetime.strptime(date_str, "%m/%d")
                    now = datetime.now()
                    meal_date = datetime(now.year, dt.month, dt.day)
                    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    if meal_date < today:
                        continue
                except ValueError:
                    continue

                # 先解析此日期所有餐次的完整日期
                parts = date_str.split("/")
                full_date = f"{datetime.now().year}-{int(parts[0]):02d}-{int(parts[1]):02d}"

                # 先访问订餐主页，初始化 session 状态
                self.client.visit_shop_page(full_date, 2)
                time.sleep(0.3)

                # 餐次轮换：不同日期从不同餐次开始尝试，确保各餐都有机会
                # row_idx % 3  → 0:早→午→晚  1:午→晚→早  2:晚→早→午
                meal_start = row_idx % 3
                for offset in range(3):
                    meal_idx = (meal_start + offset) % 3
                    meal_code = meal_idx + 1
                    meal_name = MEAL_NAMES.get(meal_code, "?")

                    if ordered_count >= max_meals:
                        break
                    status = row["meals"][meal_idx]
                    if status != "未订":
                        continue

                    key = (date_str, meal_code)
                    if key in self._ordered_set:
                        continue

                    # === 餐次调整 + 刷新 ===
                    # 第一步：调整餐次选项卡（模拟点击 早餐/午餐/晚餐）
                    msg_adjust = self.logger.add(
                        f"🔄 调整餐次 → {full_date} {meal_name}", "INFO"
                    )
                    if on_log:
                        on_log(msg_adjust)
                    self.client.visit_shop_page(full_date, meal_code)
                    time.sleep(0.3)

                    # 第二步：刷新当前餐次的菜单
                    msg_refresh = self.logger.add(
                        f"🔄 刷新 {meal_name} 菜单...", "INFO"
                    )
                    if on_log:
                        on_log(msg_refresh)
                    items = self.client.get_menu(full_date, meal_code)
                    # 菜单为空时重试一次（网络抖动可能导致空返回）
                    if not items:
                        time.sleep(0.5)
                        items = self.client.get_menu(full_date, meal_code)
                    msg_done = self.logger.add(
                        f"📋 {full_date} {meal_name} 加载到 {len(items)} 种套餐", "INFO"
                    )
                    if on_log:
                        on_log(msg_done)

                    if not items:
                        msg_skip = self.logger.add(
                            f"⚠️ {full_date} {meal_name} 菜单为空（重试后仍空），跳过", "WARN"
                        )
                        if on_log:
                            on_log(msg_skip)
                        continue

                    # 4. 按志愿优先级匹配（第一志愿→第二志愿→第三志愿→兜底关键词）
                    meal_prefix = {1: "breakfast", 2: "lunch", 3: "dinner"}.get(meal_code, "lunch")
                    meal_dislikes = self.config.get("exclude_keywords", [])
                    best = None
                    chosen_priority = ""

                    # 依次尝试第一、二、三志愿
                    for priority, label in [("first", "第一志愿"), ("second", "第二志愿"), ("third", "第三志愿")]:
                        kw_key = f"{meal_prefix}_{priority}"
                        kw_list = self.config.get(kw_key, [])
                        if not kw_list:
                            continue
                        # 检查是否有匹配的菜品
                        for item in items:
                            text = (item["name"] + " " + item["description"]).lower()
                            match = any(k.lower() in text for k in kw_list)
                            if match:
                                exclude = any(ek.lower() in text for ek in meal_dislikes)
                                if not exclude:
                                    best = item
                                    chosen_priority = label
                                    msg_dbg = self.logger.add(
                                        f"{full_date} {meal_name} "
                                        f"{label}匹配成功: {item['name']} (关键词: {kw_list})", "INFO"
                                    )
                                    if on_log: on_log(msg_dbg)
                                    break
                        if best:
                            break

                    # 没有志愿匹配上 → 用午餐志愿再试一次
                    if not best and meal_prefix != "lunch":
                        for priority, label in [("first", "午餐第一志愿"), ("second", "午餐第二志愿"), ("third", "午餐第三志愿")]:
                            kw_key = f"lunch_{priority}"
                            kw_list = self.config.get(kw_key, [])
                            if not kw_list:
                                continue
                            for item in items:
                                text = (item["name"] + " " + item["description"]).lower()
                                match = any(k.lower() in text for k in kw_list)
                                if match:
                                    exclude = any(ek.lower() in text for ek in meal_dislikes)
                                    if not exclude:
                                        best = item
                                        chosen_priority = label
                                        msg_dbg = self.logger.add(
                                            f"{full_date} {meal_name} "
                                            f"用{label}匹配成功: {item['name']}", "INFO"
                                        )
                                        if on_log: on_log(msg_dbg)
                                        break
                            if best:
                                break

                                        # 所有志愿均未匹配 → 兜底选第一个可用套餐
                    if not best and items:
                        best = items[0]
                        chosen_priority = "兜底"
                        msg_dbg = self.logger.add(
                            f"{full_date} {meal_name} "
                            f"兜底选: {best['name']} ¥{best['price']}（所有志愿均未匹配）", "INFO"
                        )
                        if on_log:
                            on_log(msg_dbg)

                    # 仍然没有匹配 → 跳过
                    if not best:
                        msg_dbg = self.logger.add(
                            f"{full_date} {meal_name} "
                            f"无可用套餐，跳过", "INFO"
                        )
                        if on_log:
                            on_log(msg_dbg)
                        continue
                    # 6. 检查余额
                    info = self.client.get_user_info()
                    balance = float(info.get("balance", 0))
                    if balance < min_balance:
                        msg = self.logger.add(
                            f"余额不足 (¥{balance})，停止自动订餐", "ERROR"
                        )
                        if on_log:
                            on_log(msg)
                        self.pause(on_log)
                        if on_done:
                            on_done(False, msg)
                        return

                    # 7. 统一订购循环：先尝试已选套餐，失败后启动 AI/自动兜底
                    # 先确保 AI 配置与当前 config 同步
                    self.ai.api_key = self.config.get("ai_api_key", "")
                    self.ai.model = self.config.get("ai_model", "deepseek-v4-flash")
                    self.ai.api_url = self.config.get("ai_api_url", "https://api.deepseek.com/v1")
                    self.ai.backend = self.config.get("ai_api_type", AIEnhancer.BACKEND_OPENAI)

                    # 构建有序备选套餐列表
                    ordered = [best]  # 已选中的套餐排第一
                    seen = {best["setmno"]}

                    # 用 AI 重排序剩余套餐作为智能备选
                    ai_used = False
                    if self.ai.is_available():
                        all_kw = (self.config.get(f"{meal_prefix}_first", [])
                                + self.config.get(f"{meal_prefix}_second", [])
                                + self.config.get(f"{meal_prefix}_third", []))
                        ai_history = [m for m in self.logger.get_recent(20) if "已订" in m]
                        ai_ranked = self.ai.rerank(items, all_kw or ["套餐"], ai_history)
                        if ai_ranked:
                            for ai_item in ai_ranked:
                                if ai_item["setmno"] not in seen:
                                    ordered.append(ai_item)
                                    seen.add(ai_item["setmno"])
                            if len(ordered) > 1:
                                ai_used = True

                    # 追加剩余未覆盖的套餐
                    for item in items:
                        if item["setmno"] not in seen:
                            ordered.append(item)
                            seen.add(item["setmno"])

                    # 逐个尝试
                    cart_success = False
                    for try_idx, candidate in enumerate(ordered):
                        if try_idx > 0:
                            label = "AI推荐" if ai_used else "自动兜底"
                            msg_try = self.logger.add(
                                f"{full_date} {meal_name} "
                                f"备选{try_idx} → {candidate['name']} ¥{candidate['price']} ({label})", "INFO"
                            )
                            if on_log: on_log(msg_try)

                        # 加购物车
                        cr = self.client.add_to_cart(
                            candidate["setmno"], full_date, meal_code
                        )
                        if not cr.get("flag"):
                            err_msg = cr.get("msg", "")
                            if try_idx == 0:
                                msg = self.logger.add(
                                    f"{full_date} {meal_name} 加购失败: {err_msg}", "ERROR"
                                )
                                if on_log: on_log(msg)
                            if "已订过此餐" in err_msg:
                                date_done_msg = self.logger.add(
                                    f"📌 {full_date} {meal_name} 已订过，继续尝试其它餐次", "INFO"
                                )
                                if on_log: on_log(date_done_msg)
                                break  # 当天已不能再订
                            continue  # 换下一个套餐

                        # 提交订单
                        o = self.client.submit_order()
                        if o.get("flag"):
                            best = candidate
                            chosen_priority = label if try_idx > 0 else chosen_priority
                            cart_success = True
                            self._ordered_set.add(key)
                            ordered_count += 1
                            msg = self.logger.add(
                                f"{full_date} {meal_name} "
                                f"已订 {best['name']} ¥{best['price']} "
                                f"({chosen_priority})", "SUCCESS"
                            )
                            if on_log: on_log(msg)
                            msg_done = self.logger.add(
                                f"📌 {full_date} {meal_name} 订餐完成，继续尝试当天其它餐次", "INFO"
                            )
                            if on_log: on_log(msg_done)
                            break
                        else:
                            err = o.get("mng", o.get("msg", "下单失败"))
                            msg = self.logger.add(
                                f"{full_date} {meal_name} "
                                f"下单失败({candidate['name']}): {err}", "ERROR"
                            )
                            if on_log: on_log(msg)
                            # 换下一个套餐继续试

                    if not cart_success:
                        continue

            if ordered_count == 0:
                msg = self.logger.add("本次检查: 没有需要订的新餐次", "INFO")
                if on_log:
                    on_log(msg)

            if on_done:
                on_done(True, f"完成，共订 {ordered_count} 餐")

        except Exception as e:
            msg = self.logger.add(f"检查异常: {e}", "ERROR")
            if on_log:
                on_log(msg)
            if on_done:
                on_done(False, str(e))


# ==================== AI 订餐聊天助手 ====================

MEMORY_FILE = os.path.join(os.path.dirname(__file__), "chat_memory.json")


class ChatMemory:
    """持久记忆 —— AI 记住用户的口味偏好（支持按餐次分类）"""

    MEAL_KEYS = {1: "breakfast", 2: "lunch", 3: "dinner"}
    MEAL_NAMES_MEM = {1: "早餐", 2: "午餐", 3: "晚餐"}

    def __init__(self):
        self.data = self._load()

    def _load(self):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {
                "likes": [],
                "dislikes": [],
                "meal_prefs": {},   # {"breakfast": {"likes":[], "dislikes":[]}, ...}
                "frequent_orders": {},
                "notes": "",
                "facts": [],
            }

    def save(self):
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def update(self, key, value):
        """更新记忆项"""
        if key in self.data:
            if isinstance(self.data[key], list):
                if isinstance(value, list):
                    self.data[key] = list(set(self.data[key] + value))
                elif value not in self.data[key]:
                    self.data[key].append(value)
            else:
                self.data[key] = value
        else:
            if isinstance(value, list):
                self.data[key] = value
            else:
                self.data[key] = value
        self.save()

    def update_meal_pref(self, meal_code, pref_type, items):
        """更新某餐次的偏好  meal_code: 1/2/3  pref_type: 'likes'/'dislikes'
           同时同步到 auto_order_config.json 供自动订餐使用"""
        meal_key = self.MEAL_KEYS.get(meal_code, "lunch")
        if "meal_prefs" not in self.data:
            self.data["meal_prefs"] = {}
        if meal_key not in self.data["meal_prefs"]:
            self.data["meal_prefs"][meal_key] = {"likes": [], "dislikes": []}
        if pref_type not in self.data["meal_prefs"][meal_key]:
            self.data["meal_prefs"][meal_key][pref_type] = []

        existing = self.data["meal_prefs"][meal_key][pref_type]
        for item in items:
            if item not in existing:
                existing.append(item)
        self.save()

        # 同步到 auto_order_config.json
        config = load_config()
        kw_key = {1: "breakfast_keywords", 2: "lunch_keywords", 3: "dinner_keywords"}
        cfg_key = kw_key.get(meal_code)
        if cfg_key and pref_type == "likes":
            config[cfg_key] = list(self.data["meal_prefs"][meal_key].get("likes", []))
        elif cfg_key and pref_type == "dislikes":
            config["exclude_keywords"] = list(
                set(config.get("exclude_keywords", []) + items)
            )
        save_config(config)

    def get_meal_keywords(self, meal_code):
        """获取某餐次的关键词（含通用关键词兜底）"""
        meal_key = self.MEAL_KEYS.get(meal_code, "lunch")
        meal_prefs = self.data.get("meal_prefs", {})
        mp = meal_prefs.get(meal_key, {})
        likes = mp.get("likes", []) or self.data.get("likes", [])
        dislikes = mp.get("dislikes", []) or self.data.get("dislikes", [])
        return likes, dislikes

    def get_summary(self):
        """生成记忆摘要"""
        parts = []

        # 通用偏好
        if self.data.get("likes"):
            parts.append(f"😊 爱吃的：{'、'.join(self.data['likes'])}")
        if self.data.get("dislikes"):
            parts.append(f"😕 不吃的：{'、'.join(self.data['dislikes'])}")

        # 按餐次偏好
        meal_prefs = self.data.get("meal_prefs", {})
        for meal_key, name in [("breakfast", "🌅 早餐"), ("lunch", "☀️ 午餐"), ("dinner", "🌙 晚餐")]:
            mp = meal_prefs.get(meal_key, {})
            if mp.get("likes"):
                parts.append(f"{name}爱吃：{'、'.join(mp['likes'])}")
            if mp.get("dislikes"):
                parts.append(f"{name}不吃：{'、'.join(mp['dislikes'])}")

        if self.data.get("frequent_orders"):
            summary = "、".join(
                f"{k}订了{v}次" for k, v in self.data["frequent_orders"].items()
            )
            parts.append(f"📊 常订：{summary}")
        if self.data.get("notes"):
            parts.append(f"📝 {self.data['notes']}")
        if self.data.get("facts"):
            parts.append(f"🧠 {'. '.join(self.data['facts'][-5:])}")
        return "\n".join(parts) if parts else "暂无记录"


CHAT_SYSTEM_PROMPT = """你是磐安中学智慧食堂的AI订餐助手，名叫「小食」。你和用户通过对话完成订餐。

**当前用户信息：**
- 姓名：{user_name}
- 账户余额：¥{balance}
- 已订餐情况：{ordered_summary}

**关于用户的记忆（AI自动保存）：**
{memory}

**可执行操作（必须用以下JSON格式返回）：**

1. 查询菜单 → {{"action": "query_menu", "date": "YYYY-MM-DD", "meal": 1或2或3}}
   （1=早餐 2=午餐 3=晚餐）

2. 加入购物车 → {{"action": "add_to_cart", "setmno": "套餐编号", "date": "YYYY-MM-DD", "meal": 1或2或3, "name": "套餐名", "price": 价格}}

3. 提交订单 → {{"action": "submit_order"}}

4. 查看购物车 → {{"action": "view_cart"}}

5. 查看订餐记录 → {{"action": "view_orders"}}

6. **记住用户偏好 → {{"action": "save_memory", "key": "likes/dislikes/notes/facts", "value": "要记住的内容"}}**
   当用户说出自己的口味偏好时，用这个保存。
   - "likes" 存爱吃的（如 ["排骨","牛肉"]）
   - "dislikes" 存不吃的（如 ["辣椒"]）
   - "notes" 存备注（如 "饭量大要多一点"）
   - "facts" 存事实描述（如 "用户说最喜欢红烧排骨"）

7. 纯聊天回复 → {{"action": "chat", "message": "你的回复内容"}}

**重要规则：**
- 用户说出口味偏好时，要用 save_memory 记住！
- 用户想订餐时，先查菜单再推荐，确认后加购并下单。
- 每次最多返回一个操作。
- 务必返回合法的JSON，不要包含其他文字。
- 语气友好，像个热心的食堂阿姨/叔叔。"""


class ChatAssistant:
    """AI 订餐聊天助手（带持久记忆）"""

    def __init__(self, client, ai_enhancer, first_order_date="2026-06-11"):
        self.client = client
        self.ai = ai_enhancer
        self.memory = ChatMemory()
        self.conversation = []
        self.user_name = ""
        self.balance = 0.0
        self.ordered_summary = "暂无"
        self.first_order_date = first_order_date

    def _get_context(self):
        """获取当前上下文"""
        try:
            info = self.client.get_user_info()
            self.user_name = info.get("name", "用户")
            self.balance = float(info.get("balance", 0))
        except Exception:
            pass
        try:
            orders = self.client.get_order_records()
            if orders:
                recent = orders[-5:]
                self.ordered_summary = "、".join(
                    f"{o['date']} {o['name']}" for o in recent
                )
        except Exception:
            pass
        return {
            "user_name": self.user_name,
            "balance": self.balance,
            "ordered_summary": self.ordered_summary,
            "memory": self.memory.get_summary(),
        }

    def _call_ai(self, prompt):
        """调用AI，返回文本"""
        if not self.ai.is_available():
            return None

        try:
            if self.ai.backend == "anthropic" and HAS_ANTHROPIC:
                return self.ai._call_anthropic(prompt)
            else:
                return self.ai._call_openai(prompt)
        except Exception:
            return None

    def chat(self, user_message):
        """处理用户消息，返回 (回复文本, 动作描述)"""
        ctx = self._get_context()

        # 尝试 AI 在线回复
        if self.ai.is_available():
            system = CHAT_SYSTEM_PROMPT.format(
                user_name=ctx["user_name"],
                balance=ctx["balance"],
                ordered_summary=ctx["ordered_summary"],
                memory=ctx["memory"],
            )
            history = "\n".join(self.conversation[-6:])
            prompt = f"{system}\n\n--- 对话历史 ---\n{history}\n\n--- 用户最新消息 ---\n{user_message}\n\n--- 请返回JSON ---"
            reply = self._call_ai(prompt)
            if reply:
                action = self._parse_action(reply)
                if action:
                    self.conversation.append(f"用户: {user_message}")
                    self.conversation.append(f"助手: {action.get('message', reply)}")
                    return self._execute_action(action)
                return self._clean_reply(reply), None

        # AI 不可用 → 本地离线模式
        return self._local_chat(user_message)

    def _local_chat(self, text):
        """离线模式：用关键词匹配理解用户意图"""
        t = text.strip()
        ctx = self._get_context()

        # --- 问候 ---
        if any(w in t for w in ["你好", "嗨", "hi", "hello", "在吗", "在不在"]):
            return (
                f"你好呀！我是「小食」🤖\n"
                f"当前余额 ¥{ctx['balance']}\n"
                f"你可以说：\n"
                f"• 明天午餐有什么？\n"
                f"• 帮我订套餐A\n"
                f"• 帮我下单\n"
                f"• 看看购物车\n"
                f"• 我喜欢吃排骨",
                "chat",
            )

        # --- 查看余额 ---
        if any(w in t for w in ["余额", "多少钱", "还有多少"]):
            return f"💰 当前账户余额：**¥{ctx['balance']}**", "chat"

        # --- 先提取餐次 ---
        meal_keyword_map = {"早餐": 1, "午餐": 2, "晚餐": 3, "中餐": 2, "晚饭": 3}
        detected_meal = None
        for name, code in meal_keyword_map.items():
            if name in t:
                detected_meal = code
                break

        # --- 按志愿设置（第一/二/三志愿）---
        priority_map = {"第一志愿": "first", "第二志愿": "second", "第三志愿": "third",
                        "1志愿": "first", "2志愿": "second", "3志愿": "third",
                        "志愿1": "first", "志愿2": "second", "志愿3": "third"}
        detected_priority = None
        for pname, pkey in priority_map.items():
            if pname in t:
                detected_priority = pkey
                break

        if detected_priority and detected_meal:
            # 提取关键词
            for kw in ["喜欢", "爱吃", "爱喝", "吃", "订"]:
                if kw in t:
                    raw = t.split(kw)[-1].strip().strip("，。,.！!")
                    raw = re.sub(r"^(吃|喝|点|订)\s*", "", raw)
                    if raw:
                        items_list = [x.strip() for x in re.split(r"[，,、和与及\s+]", raw) if x.strip()]
                        improved = []
                        for item in items_list:
                            if len(item) >= 3 and not any(c in item for c in "，,、和与及"):
                                parts = re.findall(r'[一-鿿]{1,2}', item)
                                if len(parts) > 1:
                                    improved.extend(parts)
                                else:
                                    improved.append(item)
                            else:
                                improved.append(item)
                        # 保存到 auto_order_config.json
                        prefix = {1: "breakfast", 2: "lunch", 3: "dinner"}.get(detected_meal, "lunch")
                        cfg_key = f"{prefix}_{detected_priority}"
                        config = load_config()
                        config[cfg_key] = improved
                        save_config(config)
                        meal_name = MEAL_NAMES.get(detected_meal, "")
                        priority_labels = {"first": "第一", "second": "第二", "third": "第三"}
                        return f"🧠 记住了！你{meal_name}{priority_labels.get(detected_priority, '')}志愿: {', '.join(improved)} 😊", "chat"
                    break

        # --- 记住偏好（支持按餐次）---
        if any(w in t for w in ["喜欢", "爱吃", "爱喝", "偏好"]):
            for kw in ["喜欢", "爱吃", "爱喝"]:
                if kw in t:
                    likes_raw = t.split(kw)[-1].strip().strip("，。,.！!")
                    likes_raw = re.sub(r"^(吃|喝|点|订)\s*", "", likes_raw)
                    if likes_raw:
                        items = [x.strip() for x in re.split(r"[，,、和与及\s+]", likes_raw) if x.strip()]
                        # 对没有分隔符的长词做2字切分（如"排骨牛肉"→["排骨","牛肉"]）
                        improved = []
                        for item in items:
                            if len(item) >= 3 and not any(c in item for c in "，,、和与及"):
                                parts = re.findall(r'[一-鿿]{1,2}', item)
                                if len(parts) > 1:
                                    improved.extend(parts)
                                else:
                                    improved.append(item)
                            else:
                                improved.append(item)
                        items = improved
                        if detected_meal:
                            self.memory.update_meal_pref(detected_meal, "likes", items)
                            meal_name = MEAL_NAMES.get(detected_meal, "")
                            return f"🧠 记住了！你{meal_name}喜欢吃 {', '.join(items)} 😊", "chat"
                        else:
                            self.memory.update("likes", items)
                            return f"🧠 记住了！你喜欢吃 {', '.join(items)} 😊", "chat"
        if any(w in t for w in ["不吃", "讨厌", "不要"]):
            for kw in ["不吃", "讨厌", "不要"]:
                if kw in t:
                    dislikes_raw = t.split(kw)[-1].strip().strip("，。,.！!")
                    if dislikes_raw:
                        items = [x.strip() for x in re.split(r"[，,、和与及\s+]", dislikes_raw) if x.strip()]
                        improved = []
                        for item in items:
                            if len(item) >= 3 and not any(c in item for c in "，,、和与及"):
                                parts = re.findall(r'[一-鿿]{1,2}', item)
                                if len(parts) > 1:
                                    improved.extend(parts)
                                else:
                                    improved.append(item)
                            else:
                                improved.append(item)
                        items = improved
                        if detected_meal:
                            self.memory.update_meal_pref(detected_meal, "dislikes", items)
                            meal_name = MEAL_NAMES.get(detected_meal, "")
                            return f"🧠 记住了！你{meal_name}不吃 {', '.join(items)}，我注意～", "chat"
                        else:
                            self.memory.update("dislikes", items)
                            return f"🧠 记住了！你不吃 {', '.join(items)}，我以后注意～", "chat"
        if "记住" in t or "我叫" in t or "我是" in t:
            self.memory.update("facts", t)
            return "🧠 好的，我记住了！", "chat"

        # --- 查看记忆 ---
        if any(w in t for w in ["记得什么", "记住了什么", "我的记忆", "你记得"]):
            summary = ctx["memory"]
            if summary and summary != "暂无记录":
                return f"🧠 **我记得关于你的：**\n{summary}", "chat"
            return "🧠 我还没有关于你的记忆呢，跟我说说你的口味吧！", "chat"

        # --- 查看菜单 ---
        # 解析日期和餐次
        date_str, meal_code = self._parse_datetime(t)
        if any(w in t for w in ["菜单", "有什么", "吃什么", "推荐", "看看", "吃啥", "有哪些"]):
            meal_name = MEAL_NAMES.get(meal_code, "")
            try:
                items = self.client.get_menu(date_str, meal_code)
                if not items:
                    return f"📭 {date_str} {meal_name} 暂无可用套餐", "chat"
                text = f"📋 **{date_str} {meal_name}**\n"
                for i, it in enumerate(items):
                    text += f"{i+1}. {it['name']} ¥{it['price']} — {it['description'][:30]}\n"
                text += "\n想订哪个？跟我说「订套餐X」"
                return text, "chat"
            except Exception as e:
                return f"查询菜单失败：{e}", "chat"

        # --- 根据偏好推荐 ---
        if any(w in t for w in ["推荐", "吃啥"]):
            try:
                items = self.client.get_menu(date_str, meal_code)
                likes = self.memory.data.get("likes", [])
                if likes and items:
                    scored = KeywordMatcher.score(items, likes, self.memory.data.get("dislikes", []))
                    text = f"📋 **{date_str} {MEAL_NAMES.get(meal_code, '')} — 为你推荐**\n"
                    for i, it in enumerate(scored[:3]):
                        text += f"{i+1}. {it['name']} ¥{it['price']} — {it['description'][:30]}\n"
                    text += "\n想订哪个？跟我说「订套餐X」"
                    return text, "chat"
            except Exception:
                pass

        # --- 订餐 ---
        if any(w in t for w in ["订", "来一份", "加一份", "选", "要"]) and ("套餐" in t or "A" in t or "B" in t or "C" in t or "D" in t or "E" in t or "F" in t):
            # 先查菜单找套餐编号
            try:
                items = self.client.get_menu(date_str, meal_code)
                # 匹配套餐名
                target_name = None
                for name_key in ["套餐A", "套餐B", "套餐C", "套餐D", "套餐E", "套餐F"]:
                    if name_key in t:
                        target_name = name_key
                        break
                if not target_name:
                    # 尝试匹配 "A" "B" 等
                    for letter in "ABCDEF":
                        if letter in t:
                            target_name = f"套餐{letter}"
                            break
                if target_name:
                    for it in items:
                        if it["name"] == target_name:
                            result = self.client.add_to_cart(it["setmno"], date_str, meal_code)
                            if result.get("flag"):
                                # 记录常订
                                freq = self.memory.data.setdefault("frequent_orders", {})
                                freq[target_name] = freq.get(target_name, 0) + 1
                                self.memory.save()
                                return (
                                    f"✅ **{target_name}** ¥{it['price']} 已加入购物车！\n"
                                    f"📅 {date_str} {MEAL_NAMES.get(meal_code, '')}\n"
                                    f"购物车共 {result.get('num', '?')} 件，总计 ¥{result.get('sum', '?')}\n"
                                    "需要我帮你提交订单吗？",
                                    "add_to_cart",
                                )
                            return f"❌ 加购失败：{result.get('msg', '未知错误')}", "chat"
                    return f"😅 没找到 {target_name}，请确认套餐名称", "chat"
            except Exception as e:
                return f"订餐失败：{e}", "chat"

        # --- 提交订单 ---
        if any(w in t for w in ["下单", "提交订单", "结算", "付款"]):
            return self._execute_action({"action": "submit_order"})

        # --- 查看购物车 ---
        if any(w in t for w in ["购物车", "看看", "选了"]):
            return self._execute_action({"action": "view_cart"})

        # --- 订餐记录 ---
        if any(w in t for w in ["记录", "订过", "历史", "吃过"]):
            return self._execute_action({"action": "view_orders"})

        # --- 帮助 ---
        if any(w in t for w in ["帮助", "怎么用", "功能", "指令", "能做什么"]):
            return (
                "🤖 **我能帮你：**\n\n"
                "📋 **看菜单** —「明天午餐有什么？」\n"
                "🍽️ **订套餐** —「帮我订套餐A」\n"
                "🛒 **看购物车** —「看看购物车」\n"
                "✅ **提交订单** —「帮我下单」\n"
                "🧠 **记住口味** —「我喜欢吃排骨」\n"
                "💰 **查余额** —「我还有多少钱」\n"
                "📝 **订餐记录** —「我订过什么」",
                "chat",
            )

        # --- 感谢 ---
        if any(w in t for w in ["谢谢", "感谢", "谢了"]):
            return "不客气！祝用餐愉快 🍽️😊", "chat"

        # --- 默认回复 ---
        return (
            "😅 抱歉，我现在处于离线模式，能力有限。\n"
            "你可以试试：\n"
            "• 「明天午餐有什么？」\n"
            "• 「帮我订套餐A」\n"
            "• 「帮我下单」\n"
            "• 「帮助」查看全部功能",
            "chat",
        )

    def _parse_datetime(self, text):
        """从用户消息中解析日期和餐次"""
        now = datetime.now()
        # 默认：今天的下一餐
        date_str = self.first_order_date if hasattr(self, 'first_order_date') else now.strftime("%Y-%m-%d")
        meal_code = 2  # 默认午餐

        # 解析餐次
        if "早餐" in text or "早上" in text or "早晨" in text:
            meal_code = 1
        elif "午餐" in text or "中午" in text or "中饭" in text or "午饭" in text:
            meal_code = 2
        elif "晚餐" in text or "晚上" in text or "晚饭" in text:
            meal_code = 3

        # 解析日期
        today = now.strftime("%Y-%m-%d")
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        if "今天" in text or "今日" in text:
            date_str = today
        elif "明天" in text or "明日" in text:
            date_str = tomorrow
        elif "后天" in text:
            date_str = (now + timedelta(days=2)).strftime("%Y-%m-%d")
        else:
            # 尝试匹配具体日期如 06/12 或 06-12
            m = re.search(r"(\d{1,2})[/-](\d{1,2})", text)
            if m:
                date_str = f"{now.year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
            else:
                # 尝试匹配星期
                week_map = {
                    "周一": 0, "星期二": 1, "周二": 1, "星期三": 2, "周三": 2,
                    "星期四": 3, "周四": 3, "星期五": 4, "周五": 4,
                    "星期六": 5, "周六": 5, "星期日": 6, "周日": 6, "星期天": 6,
                }
                for kw, offset in week_map.items():
                    if kw in text:
                        days_ahead = offset - now.weekday()
                        if days_ahead <= 0:
                            days_ahead += 7
                        date_str = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
                        break

        # 确保日期是首订日期之后
        first = self.first_order_date if hasattr(self, 'first_order_date') else "2026-06-11"
        if date_str < first:
            date_str = first

        return date_str, meal_code

    def _parse_action(self, text):
        """从AI回复中提取JSON action"""
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None

    def _clean_reply(self, text):
        """清理回复中的JSON痕迹"""
        text = re.sub(r"\{.*?\}", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL).strip()
        return text if text else "好的，已收到！"

    def _execute_action(self, action):
        """执行AI返回的动作"""
        act = action.get("action", "chat")
        msg = action.get("message", "")

        try:
            # --- 保存记忆 ---
            if act == "save_memory":
                key = action.get("key", "")
                value = action.get("value", "")
                if key and value:
                    self.memory.update(key, value)
                    return (
                        f"🧠 记住了！" + (f" ({value})" if not isinstance(value, list) else f" {value}"),
                        act,
                    )
                return "好的，已记录！", act

            # --- 查询菜单 ---
            elif act == "query_menu":
                date = action.get("date", "")
                meal = action.get("meal", 2)
                meal_name = MEAL_NAMES.get(meal, "未知")
                items = self.client.get_menu(date, meal)
                if not items:
                    return f"📭 {date} {meal_name} 暂无可用套餐", act
                menu_list = "\n".join(
                    f"{i+1}. {it['name']} ¥{it['price']} — {it['description'][:30]}"
                    for i, it in enumerate(items)
                )
                # 把菜单发给AI生成推荐
                ctx = self._get_context()
                prompt = (
                    f"{CHAT_SYSTEM_PROMPT.format(user_name=ctx['user_name'], balance=ctx['balance'], ordered_summary=ctx['ordered_summary'], memory=ctx['memory'])}\n\n"
                    f"以下是{date} {meal_name}的菜单，请推荐给用户：\n{menu_list}\n\n"
                    f"用户说：{self.conversation[-1] if self.conversation else '看看有什么'}\n\n请返回JSON"
                )
                reply = self._call_ai(prompt)
                if reply:
                    sub_action = self._parse_action(reply)
                    if sub_action and sub_action.get("action") in ("add_to_cart", "save_memory"):
                        return self._execute_action(sub_action)
                return f"📋 **{date} {meal_name}**\n{menu_list}", act

            # --- 加入购物车 ---
            elif act == "add_to_cart":
                setmno = action.get("setmno", "")
                date = action.get("date", "")
                meal = action.get("meal", 2)
                name = action.get("name", "套餐")
                price = action.get("price", 0)

                if not setmno:
                    return "❌ 缺少套餐编号，请重新选择", act

                result = self.client.add_to_cart(setmno, date, meal)
                if result.get("flag"):
                    return (
                        f"✅ **{name}** ¥{price} 已加入购物车！\n"
                        f"📅 {date} {MEAL_NAMES.get(meal, '')}\n"
                        f"购物车共 {result.get('num', '?')} 件，总计 ¥{result.get('sum', '?')}\n"
                        "需要我帮你提交订单吗？",
                        act,
                    )
                else:
                    return f"❌ 加购失败：{result.get('msg', '未知错误')}", act

            # --- 提交订单 ---
            elif act == "submit_order":
                cart_items, total, cnt, _ = self.client.get_cart()
                if not cart_items:
                    return "🛒 购物车是空的，先选餐吧！", act

                result = self.client.submit_order()
                if result.get("flag"):
                    ccnum = result.get("ccnum", 0)
                    return (
                        f"🎉 **下单成功！** 共订 {ccnum} 个餐次，祝用餐愉快！🍽️",
                        act,
                    )
                else:
                    err = result.get("mng", result.get("msg", "下单失败"))
                    return f"❌ 下单失败：{err}", act

            # --- 查看购物车 ---
            elif act == "view_cart":
                items, total, cnt, _ = self.client.get_cart()
                if not items:
                    return "🛒 **购物车** 是空的～", act
                text = f"🛒 **购物车** ({cnt}件，总计 ¥{total})\n"
                for item in items:
                    mn = MEAL_NAMES.get(item["meal_code"], "?")
                    text += f"• 套餐 {item['setmno']} × {item['quantity']}份 ({item['date_str']} {mn})\n"
                return text, act

            # --- 查看订单 ---
            elif act == "view_orders":
                orders = self.client.get_order_records()
                if not orders:
                    return "📝 暂无订餐记录", act
                text = f"📝 **最近订餐**（共{len(orders)}条）\n"
                for o in orders[-8:]:
                    text += f"• {o['date']} — {o['name']} ¥{o['price']}\n"
                total = sum(o["price"] for o in orders)
                text += f"💰 总计消费：¥{total}"
                return text, act

            else:
                return msg or "好的，已收到！", act

        except Exception as e:
            return f"操作执行异常：{e}", act


# ==================== GUI 应用程序 ====================
class CanteenApp:
    def __init__(self, root):
        self.root = root
        self.root.title("磐安中学智慧食堂 - 订餐系统")
        self.root.geometry("900x650")
        self.root.minsize(800, 550)

        # 第一个可订餐日期是 2026-06-11 (周四)
        self.first_order_date = "2026-06-11"

        # 多账号支持
        self._accounts, self._active_idx = AccountsManager.load()
        self._setup_client_for_active_account()

        self.current_date = self.first_order_date
        self.current_meal = 2  # 默认午餐
        self.menu_items = []
        self.cart_items = []
        self.cart_total = 0.0
        self.cart_count = 0

        # 设置样式
        self.style = ttk.Style()
        self.style.theme_use("clam")
        self.style.configure("Accent.TButton", background="#2E8B57", foreground="white")
        self.style.configure("Danger.TButton", background="#E74C3C", foreground="white")
        self.style.configure("Warning.TButton", background="#FF8C00", foreground="white")

        self._build_ui()
        self._do_login()

    def _setup_client_for_active_account(self, idx=None):
        """按激活账号创建/重建 client 和 engine"""
        if idx is not None:
            self._active_idx = idx
        account = self._accounts[self._active_idx]

        # 用账号凭证创建 client
        self.client = CanteenClient(
            username=account["username"],
            password=account["password"],
            g_id=account.get("g_id", G_ID),
        )
        # 重建 engine
        self.auto_engine = AutoOrderEngine(self.client)
        # 同步 AI
        self.chat_assistant = ChatAssistant(
            self.client, self.auto_engine.ai,
            first_order_date=self.first_order_date,
        )

    def _switch_account(self, idx):
        """切换账号（GUI 回调）"""
        if idx == self._active_idx:
            return
        # 保存当前自动订餐配置
        try:
            self._on_auto_save()
        except Exception:
            pass
        AccountsManager.save(self._accounts, idx)
        self._active_idx = idx
        self._setup_client_for_active_account(idx)
        # 刷新自动订餐页面的输入框为当前账号的配置
        self._refresh_auto_order_ui()
        self.client.logged_in = False
        # 重新登录
        self.footer_label.config(text=f"正在切换账号...")
        threading.Thread(target=self._relogin, daemon=True).start()

    def _refresh_auto_order_ui(self):
        """根据当前 engine 的 config 刷新自动订餐页面的所有输入框"""
        cfg = self.auto_engine.config
        # 通用关键词
        self.auto_keywords_var.set(", ".join(cfg.get("keywords", [])))
        self.auto_exclude_var.set(", ".join(cfg.get("exclude_keywords", [])))
        # 按餐次志愿
        for prefix in ["breakfast", "lunch", "dinner"]:
            for level in ["first", "second", "third"]:
                key = f"{prefix}_{level}"
                values = cfg.get(key, [])
                for slot_i in range(3):
                    var_name = f"auto_{key}_var_{slot_i}"
                    var = getattr(self, var_name, None)
                    if var is not None:
                        var.set(values[slot_i] if slot_i < len(values) else "")
        # 其他设置
        self.auto_interval_var.set(str(cfg.get("interval_minutes", 30)))
        self.auto_max_var.set(str(cfg.get("max_meals_per_day", 6)))
        self.auto_ai_var.set(cfg.get("ai_enabled", False))
        self.auto_ai_key_var.set(cfg.get("ai_api_key", ""))
        self.auto_ai_model_var.set(cfg.get("ai_model", "deepseek-v4-flash"))
        api_type = cfg.get("ai_api_type", "openai")
        self.auto_ai_type_var.set("Anthropic (Claude)" if api_type == "anthropic" else "OpenAI 兼容 (DeepSeek)")
        self.auto_ai_url_var.set(cfg.get("ai_api_url", "https://api.deepseek.com/v1"))

    def _relogin(self):
        """重新登录并刷新界面"""
        success, msg = self.client.login()
        self.root.after(0, lambda: self._on_login_result(success, msg))

    def _on_account_selected(self, event=None):
        """账号下拉框选择事件"""
        name = self.account_var.get()
        for i, a in enumerate(self._accounts):
            if a["name"] == name:
                self._switch_account(i)
                break

    def _on_add_account(self, event=None):
        """弹出添加账号对话框（含一键创建 GitHub 仓库）"""
        dialog = tk.Toplevel(self.root)
        dialog.title("添加账号")
        dialog.geometry("520x430")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 520) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 430) // 2
        dialog.geometry(f"+{x}+{y}")

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="添加新食堂账号", font=("Microsoft YaHei", 13, "bold")).grid(
            row=0, column=0, columnspan=3, pady=(0, 12), sticky="w")

        # 输入字段
        entries = {}
        fields = [
            ("name",     "姓名 *",           "账号显示名称，如\"张三\""),
            ("username", "身份证号/用户名 *", "登录食堂系统的用户名"),
            ("password", "密码 *",           "登录密码"),
            ("g_id",     "G_ID（学校标识）", "默认即可"),
        ]
        for i, (key, label, ph) in enumerate(fields):
            r = i + 1
            ttk.Label(frame, text=label, font=("Microsoft YaHei", 9)).grid(
                row=r, column=0, sticky="w", pady=4)
            var = tk.StringVar()
            if key == "g_id":
                var.set("3C50E0296E13C2467986A75791000008")
            elif key == "password":
                var.set("")
            entry = ttk.Entry(frame, textvariable=var, width=40, font=("Microsoft YaHei", 9))
            if key == "password":
                entry.config(show="*")
            entry.grid(row=r, column=1, columnspan=2, sticky="ew", pady=4, padx=(8, 0))
            entries[key] = var

        # GitHub 仓库行：输入框 + 创建按钮
        repo_row = len(fields) + 1
        ttk.Label(frame, text="GitHub 仓库", font=("Microsoft YaHei", 9)).grid(
            row=repo_row, column=0, sticky="w", pady=4)

        # 自动生成仓库名
        repo_num = len(self._accounts) + 1
        default_repo = f"Minhao-Chen-byte/pazx-canteen-order-{repo_num}" if repo_num > 1 else "Minhao-Chen-byte/pazx-canteen-order"
        entries["github_repo"] = tk.StringVar(value=default_repo)
        repo_entry = ttk.Entry(frame, textvariable=entries["github_repo"], width=25,
                               font=("Microsoft YaHei", 9))
        repo_entry.grid(row=repo_row, column=1, sticky="ew", pady=4, padx=(8, 0))

        # 进度标签
        progress_var = tk.StringVar(value="")
        progress_label = ttk.Label(frame, textvariable=progress_var,
                                   font=("Microsoft YaHei", 8), foreground="#2E8B57")
        progress_label.grid(row=repo_row + 1, column=0, columnspan=3, pady=(2, 6), sticky="w")

        def _get_github_token():
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            if token:
                return token
            token_file = os.path.join(os.path.dirname(__file__), ".github_token")
            try:
                with open(token_file, "r") as f:
                    return f.read().strip()
            except FileNotFoundError:
                return None

        def _api_gh(method, url, data=None):
            token = _get_github_token()
            if not token:
                raise Exception("未找到 GitHub Token")
            req = urllib.request.Request(url, method=method)
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Accept", "application/vnd.github+json")
            if data is not None:
                body = json.dumps(data).encode()
                req.add_header("Content-Type", "application/json")
                req.data = body
            resp = urllib.request.urlopen(req, timeout=30)
            result = resp.read().decode()
            return json.loads(result) if result.strip() else {}

        def do_create_repo():
            repo_full = entries["github_repo"].get().strip()
            if not repo_full or "/" not in repo_full:
                progress_var.set("❌ 仓库格式错误，如 Minhao-Chen-byte/pazx-canteen-order")
                return
            parts = repo_full.split("/")
            owner, repo_name = parts[0], parts[1]

            try:
                progress_var.set("⏳ 创建 GitHub 仓库...")
                dialog.update()

                # 1. 创建仓库（空仓库，不含 README）
                repo_created = False
                try:
                    _api_gh("POST", "https://api.github.com/user/repos", {
                        "name": repo_name, "private": False,
                        "description": "磐安中学智慧食堂 - 自动订餐",
                        "auto_init": False,
                    })
                    repo_created = True
                except urllib.error.HTTPError as e:
                    if e.status == 422:
                        progress_var.set("⚠️ 仓库已存在，使用现有仓库")
                    else:
                        raise

                # 2. 推送代码文件（先初始化再批量推送）
                progress_var.set("⏳ 推送代码文件...")
                dialog.update()

                SCRIPT_DIR = os.path.dirname(__file__)
                CODE_FILES = [
                    ".github/workflows/auto-order.yml",
                    ".github/scripts/write_config.py",
                    "auto_order_daemon.py",
                    "canting_order.py",
                    "requirements.txt",
                    ".gitignore",
                ]

                # 判断仓库是否为空，空则先用 Contents API 初始化
                try:
                    _api_gh("GET", f"https://api.github.com/repos/{repo_full}/git/refs/heads/main")
                    branch = "main"
                except Exception:
                    try:
                        _api_gh("GET", f"https://api.github.com/repos/{repo_full}/git/refs/heads/master")
                        branch = "master"
                    except Exception:
                        # 空仓库：用 Contents API 创建 .gitignore 初始化
                        gitignore_path = os.path.join(SCRIPT_DIR, ".gitignore")
                        if os.path.exists(gitignore_path):
                            with open(gitignore_path, "r", encoding="utf-8") as f:
                                gi_content = f.read()
                        else:
                            gi_content = "__pycache__/\n*.pyc\n"
                        _api_gh("PUT", f"https://api.github.com/repos/{repo_full}/contents/.gitignore", {
                            "message": "Initialize repo",
                            "content": base64.b64encode(gi_content.encode()).decode(),
                        })
                        # 新仓库默认分支是 main
                        branch = "main"

                # 获取当前 HEAD 和 tree
                head = _api_gh("GET", f"https://api.github.com/repos/{repo_full}/git/refs/heads/{branch}")
                head_sha = head["object"]["sha"]
                base_tree = _api_gh("GET",
                    f"https://api.github.com/repos/{repo_full}/git/commits/{head_sha}")["tree"]["sha"]

                # 用 Git API 批量推送其余文件
                tree_entries = []
                for fname in CODE_FILES:
                    fpath = os.path.join(SCRIPT_DIR, fname)
                    if not os.path.exists(fpath):
                        continue
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                    blob = _api_gh("POST",
                        f"https://api.github.com/repos/{repo_full}/git/blobs",
                        {"content": content, "encoding": "utf-8"})
                    tree_entries.append({
                        "path": fname, "mode": "100644",
                        "type": "blob", "sha": blob["sha"],
                    })

                tree = _api_gh("POST",
                    f"https://api.github.com/repos/{repo_full}/git/trees",
                    {"base_tree": base_tree, "tree": tree_entries})
                new_commit = _api_gh("POST",
                    f"https://api.github.com/repos/{repo_full}/git/commits",
                    {"message": "磐安中学智慧食堂自动订餐系统",
                     "tree": tree["sha"], "parents": [head_sha]})
                _api_gh("PATCH",
                    f"https://api.github.com/repos/{repo_full}/git/refs/heads/{branch}",
                    {"sha": new_commit["sha"]})

                progress_var.set("✓ 代码已推送")

                # 3. 设置 Secrets
                progress_var.set("⏳ 配置 Secrets...")
                dialog.update()

                # 获取公钥
                pk = _api_gh("GET",
                    f"https://api.github.com/repos/{repo_full}/actions/secrets/public-key")
                key_id = pk["key_id"]

                from nacl import encoding, public as nacl_public
                def encrypt_secret(pubkey_b64, val):
                    pk_obj = nacl_public.PublicKey(pubkey_b64.encode(), encoding.Base64Encoder())
                    return base64.b64encode(
                        nacl_public.SealedBox(pk_obj).encrypt(val.encode())
                    ).decode()

                def set_secret(name, value):
                    _api_gh("PUT",
                        f"https://api.github.com/repos/{repo_full}/actions/secrets/{name}",
                        {"encrypted_value": encrypt_secret(pk["key"], value),
                         "key_id": key_id})

                gh_pat = _get_github_token()
                username = entries["username"].get().strip()
                password = entries["password"].get().strip()
                g_id = entries["g_id"].get().strip() or "3C50E0296E13C2467986A75791000008"
                name = entries["name"].get().strip()

                # 查找或创建本地账号配置（确保关键词等设置能同步到 GitHub Secrets）
                existing = next((a for a in self._accounts if a["name"] == name), None)
                if existing:
                    cfg_path = os.path.join(os.path.dirname(__file__), existing["config_file"])
                else:
                    # 创建新账号
                    config_file = AccountsManager.generate_config_file(self._accounts + [{}])
                    new_account = {
                        "name": name, "username": username, "password": password,
                        "g_id": g_id, "config_file": config_file, "github_repo": repo_full,
                    }
                    cfg_path = os.path.join(os.path.dirname(__file__), config_file)
                    clean_config = dict(DEFAULT_CONFIG)
                    clean_config["keywords"] = []
                    with open(cfg_path, "w", encoding="utf-8") as f:
                        json.dump(clean_config, f, ensure_ascii=False, indent=2)
                    self._accounts.append(new_account)
                    AccountsManager.save(self._accounts, self._active_idx)

                # 从本地配置文件读取关键词，同步到 GitHub Secrets
                cfg = {}
                if os.path.exists(cfg_path):
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)

                def _kw(keys):
                    return ",".join(keys) if keys else ""

                secrets_to_set = {
                    "GH_PAT": gh_pat,
                    "CANTEEN_USERNAME": username,
                    "CANTEEN_PASSWORD": password,
                    "CANTEEN_G_ID": g_id,
                    "KEYWORDS": _kw(cfg.get("keywords", [])),
                    "EXCLUDE_KEYWORDS": _kw(cfg.get("exclude_keywords", [])),
                    "BREAKFAST_FIRST": _kw(cfg.get("breakfast_first", [])),
                    "BREAKFAST_SECOND": _kw(cfg.get("breakfast_second", [])),
                    "BREAKFAST_THIRD": _kw(cfg.get("breakfast_third", [])),
                    "LUNCH_FIRST": _kw(cfg.get("lunch_first", [])),
                    "LUNCH_SECOND": _kw(cfg.get("lunch_second", [])),
                    "LUNCH_THIRD": _kw(cfg.get("lunch_third", [])),
                    "DINNER_FIRST": _kw(cfg.get("dinner_first", [])),
                    "DINNER_SECOND": _kw(cfg.get("dinner_second", [])),
                    "DINNER_THIRD": _kw(cfg.get("dinner_third", [])),
                    "AI_API_KEY": cfg.get("ai_api_key", ""),
                }

                ok = 0
                for name, value in secrets_to_set.items():
                    try:
                        set_secret(name, value)
                        ok += 1
                    except Exception as e:
                        progress_var.set(f"⚠️ Secret {name} 失败: {e}")

                progress_var.set(f"✓ Secrets 已配置 ({ok}/{len(secrets_to_set)})")

                # 4. 触发首次运行（使用检测到的分支名）
                progress_var.set("⏳ 启动自我维持链...")
                dialog.update()
                _api_gh("POST",
                    f"https://api.github.com/repos/{repo_full}/actions/workflows/auto-order.yml/dispatches",
                    {"ref": branch})
                progress_var.set("✅ 仓库已就绪，自动订餐链已启动！")

            except Exception as e:
                progress_var.set(f"❌ 失败: {e}")
                return

        create_btn = ttk.Button(frame, text="🚀 创建仓库", command=lambda: threading.Thread(
            target=do_create_repo, daemon=True).start())
        create_btn.grid(row=repo_row, column=2, padx=(5, 0), pady=4)

        def do_add():
            name = entries["name"].get().strip()
            username = entries["username"].get().strip()
            password = entries["password"].get().strip()
            g_id = entries["g_id"].get().strip() or "3C50E0296E13C2467986A75791000008"
            repo = entries["github_repo"].get().strip()

            if not name or not username or not password:
                messagebox.showwarning("缺少信息", "姓名、身份证号和密码为必填项", parent=dialog)
                return
            if any(a["name"] == name for a in self._accounts):
                messagebox.showwarning("重复", f"账号\"{name}\"已存在", parent=dialog)
                return

            config_file = AccountsManager.generate_config_file(self._accounts + [{}])
            new_account = {
                "name": name, "username": username, "password": password,
                "g_id": g_id, "config_file": config_file, "github_repo": repo,
            }

            clean_config = dict(DEFAULT_CONFIG)
            clean_config["keywords"] = []
            cfg_path = os.path.join(os.path.dirname(__file__), config_file)
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(clean_config, f, ensure_ascii=False, indent=2)

            self._accounts.append(new_account)
            AccountsManager.save(self._accounts, self._active_idx)
            self.account_combo["values"] = [a["name"] for a in self._accounts]
            self.account_var.set(name)
            dialog.destroy()
            self._switch_account(len(self._accounts) - 1)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=repo_row + 2, column=0, columnspan=3, pady=(14, 0), sticky="e")
        ttk.Button(btn_frame, text="✓ 添加账号", command=do_add, width=12).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(btn_frame, text="取消", command=dialog.destroy, width=10).pack(side=tk.RIGHT)

    def _build_ui(self):
        """构建界面"""
        # 顶部标题栏
        header_frame = tk.Frame(self.root, bg="#2E8B57", height=50)
        header_frame.pack(fill=tk.X, side=tk.TOP)
        header_frame.pack_propagate(False)

        tk.Label(
            header_frame,
            text="🍽️ 磐安中学智慧食堂",
            bg="#2E8B57",
            fg="white",
            font=("Microsoft YaHei", 16, "bold"),
        ).pack(side=tk.LEFT, padx=15, pady=8)

        self.status_label = tk.Label(
            header_frame,
            text="正在登录...",
            bg="#2E8B57",
            fg="white",
            font=("Microsoft YaHei", 10),
        )
        self.status_label.pack(side=tk.RIGHT, padx=(5, 10), pady=8)

        # 账号切换下拉框
        tk.Label(header_frame, text="👤", bg="#2E8B57", fg="white",
                 font=("Microsoft YaHei", 11)).pack(side=tk.RIGHT, padx=(10, 2))
        self.account_var = tk.StringVar(
            value=self._accounts[self._active_idx]["name"]
        )
        self.account_combo = ttk.Combobox(
            header_frame,
            textvariable=self.account_var,
            values=[a["name"] for a in self._accounts],
            state="readonly",
            width=10,
            font=("Microsoft YaHei", 9),
        )
        self.account_combo.pack(side=tk.RIGHT, padx=(0, 5), pady=10)
        self.account_combo.bind("<<ComboboxSelected>>", self._on_account_selected)

        # 添加账号按钮
        self.add_account_btn = tk.Label(
            header_frame, text="➕", bg="#2E8B57", fg="white",
            font=("Microsoft YaHei", 11), cursor="hand2",
        )
        self.add_account_btn.pack(side=tk.RIGHT, padx=(0, 10), pady=10)
        self.add_account_btn.bind("<Button-1>", self._on_add_account)

        # 用户信息栏
        self.info_frame = tk.Frame(self.root, bg="#f0f8f0", height=35)
        self.info_frame.pack(fill=tk.X, side=tk.TOP)
        self.info_frame.pack_propagate(False)

        self.user_label = tk.Label(
            self.info_frame, text="", bg="#f0f8f0", font=("Microsoft YaHei", 9)
        )
        self.user_label.pack(side=tk.LEFT, padx=15, pady=5)

        self.balance_label = tk.Label(
            self.info_frame, text="", bg="#f0f8f0", font=("Microsoft YaHei", 9)
        )
        self.balance_label.pack(side=tk.RIGHT, padx=15, pady=5)

        # 主内容区域 - 使用 Notebook 标签页
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 标签页1: 订餐
        self.tab_order = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_order, text="📋 订餐")
        self._build_order_tab()

        # 标签页2: 购物车
        self.tab_cart = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_cart, text="🛒 购物车")
        self._build_cart_tab()

        # 标签页3: 结算统计
        self.tab_settle = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_settle, text="📊 结算统计")
        self._build_settle_tab()

        # 标签页4: 订餐记录
        self.tab_records = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_records, text="📝 订餐记录")
        self._build_records_tab()

        # 标签页5: 个人信息
        self.tab_profile = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_profile, text="👤 个人信息")
        self._build_profile_tab()

        # 标签页6: 自动订餐
        self.tab_auto = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_auto, text="🤖 自动订餐")
        self._build_auto_order_tab()

        # 标签页7: AI 订餐助手
        self.tab_chat = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_chat, text="💬 AI 订餐助手")
        self._build_chat_tab()

        # 底部状态栏
        status_bar = tk.Frame(self.root, bg="#ddd", height=25)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        status_bar.pack_propagate(False)

        self.footer_label = tk.Label(
            status_bar,
            text="就绪",
            bg="#ddd",
            font=("Microsoft YaHei", 8),
            anchor=tk.W,
        )
        self.footer_label.pack(fill=tk.BOTH, padx=10)

    def _build_order_tab(self):
        """构建订餐标签页"""
        # 顶部选择栏
        select_frame = tk.Frame(self.tab_order, bg="#fff", height=50)
        select_frame.pack(fill=tk.X, padx=5, pady=5)
        select_frame.pack_propagate(False)

        tk.Label(select_frame, text="日期:", font=("Microsoft YaHei", 10)).pack(
            side=tk.LEFT, padx=5
        )

        self.date_var = tk.StringVar(value=self.current_date)
        self.date_combo = ttk.Combobox(
            select_frame,
            textvariable=self.date_var,
            width=12,
            state="readonly",
            font=("Microsoft YaHei", 10),
        )
        # 生成可订餐日期（从第一个有菜单的日期开始）
        dates = []
        first_date = datetime.strptime(self.first_order_date, "%Y-%m-%d")
        for i in range(14):
            d = first_date + timedelta(days=i)
            dates.append(d.strftime("%Y-%m-%d"))
        self.date_combo["values"] = dates
        self.date_combo.pack(side=tk.LEFT, padx=5)

        tk.Label(select_frame, text="餐次:", font=("Microsoft YaHei", 10)).pack(
            side=tk.LEFT, padx=(15, 5)
        )

        self.meal_var = tk.StringVar(value="午餐")
        self.meal_combo = ttk.Combobox(
            select_frame,
            textvariable=self.meal_var,
            values=["早餐", "午餐", "晚餐"],
            width=8,
            state="readonly",
            font=("Microsoft YaHei", 10),
        )
        self.meal_combo.pack(side=tk.LEFT, padx=5)

        ttk.Button(
            select_frame,
            text="🔍 查看菜单",
            command=self._on_query_menu,
            style="Accent.TButton",
        ).pack(side=tk.LEFT, padx=15)

        ttk.Button(
            select_frame,
            text="🔄 刷新",
            command=self._on_query_menu,
        ).pack(side=tk.LEFT, padx=5)

        self.order_status_label = tk.Label(
            select_frame, text="", fg="#2E8B57", font=("Microsoft YaHei", 9)
        )
        self.order_status_label.pack(side=tk.RIGHT, padx=10)

        # 菜单列表 (带滚动条)
        list_frame = tk.Frame(self.tab_order)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.menu_listbox = tk.Listbox(
            list_frame,
            yscrollcommand=scrollbar.set,
            font=("Microsoft YaHei", 10),
            selectmode=tk.SINGLE,
            activestyle=tk.NONE,
            bg="#fafafa",
            relief=tk.FLAT,
            borderwidth=2,
        )
        self.menu_listbox.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.menu_listbox.yview)

        # 底部操作栏
        action_frame = tk.Frame(self.tab_order, bg="#fff", height=45)
        action_frame.pack(fill=tk.X)
        action_frame.pack_propagate(False)

        self.menu_info_label = tk.Label(
            action_frame, text="", font=("Microsoft YaHei", 9), fg="#666"
        )
        self.menu_info_label.pack(side=tk.LEFT, padx=10)

        ttk.Button(
            action_frame,
            text="🛒 加入购物车",
            command=self._on_add_to_cart,
            style="Accent.TButton",
        ).pack(side=tk.RIGHT, padx=10)

    def _build_cart_tab(self):
        """构建购物车标签页"""
        # 购物车列表
        list_frame = tk.Frame(self.tab_cart)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.cart_listbox = tk.Listbox(
            list_frame,
            yscrollcommand=scrollbar.set,
            font=("Microsoft YaHei", 10),
            selectmode=tk.SINGLE,
            activestyle=tk.NONE,
            bg="#fafafa",
            relief=tk.FLAT,
            borderwidth=2,
        )
        self.cart_listbox.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.cart_listbox.yview)

        # 底部操作栏
        action_frame = tk.Frame(self.tab_cart, bg="#fff", height=45)
        action_frame.pack(fill=tk.X)
        action_frame.pack_propagate(False)

        self.cart_info_label = tk.Label(
            action_frame,
            text="购物车为空",
            font=("Microsoft YaHei", 10, "bold"),
            fg="#E74C3C",
        )
        self.cart_info_label.pack(side=tk.LEFT, padx=10)

        ttk.Button(
            action_frame,
            text="🗑️ 删除选中",
            command=self._on_delete_from_cart,
            style="Danger.TButton",
        ).pack(side=tk.RIGHT, padx=5)

        ttk.Button(
            action_frame,
            text="🔄 刷新购物车",
            command=self._on_refresh_cart,
        ).pack(side=tk.RIGHT, padx=5)

        ttk.Button(
            action_frame,
            text="✅ 提交订单",
            command=self._on_submit_order,
            style="Accent.TButton",
        ).pack(side=tk.RIGHT, padx=5)

    def _build_settle_tab(self):
        """构建结算统计标签页"""
        # 使用 Treeview 显示统计表
        columns = ("date", "早餐", "午餐", "晚餐")
        self.settle_tree = ttk.Treeview(
            self.tab_settle, columns=columns, show="headings", height=14
        )
        self.settle_tree.heading("date", text="日期")
        self.settle_tree.heading("早餐", text="早餐")
        self.settle_tree.heading("午餐", text="午餐")
        self.settle_tree.heading("晚餐", text="晚餐")
        self.settle_tree.column("date", width=120, anchor=tk.CENTER)
        self.settle_tree.column("早餐", width=150, anchor=tk.CENTER)
        self.settle_tree.column("午餐", width=150, anchor=tk.CENTER)
        self.settle_tree.column("晚餐", width=150, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(self.tab_settle, orient=tk.VERTICAL, command=self.settle_tree.yview)
        self.settle_tree.configure(yscrollcommand=scrollbar.set)

        self.settle_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=5)

        # 底部
        settle_bottom = tk.Frame(self.tab_settle, bg="#fff", height=40)
        settle_bottom.pack(fill=tk.X)
        settle_bottom.pack_propagate(False)

        self.settle_total_label = tk.Label(
            settle_bottom, text="", font=("Microsoft YaHei", 11, "bold"), fg="#2E8B57"
        )
        self.settle_total_label.pack(side=tk.LEFT, padx=15)

        ttk.Button(
            settle_bottom,
            text="🔄 刷新统计",
            command=self._on_refresh_settle,
        ).pack(side=tk.RIGHT, padx=(0, 5))

    def _build_records_tab(self):
        """构建订餐记录标签页"""
        list_frame = tk.Frame(self.tab_records)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.records_listbox = tk.Listbox(
            list_frame,
            yscrollcommand=scrollbar.set,
            font=("Microsoft YaHei", 10),
            bg="#fafafa",
            relief=tk.FLAT,
            borderwidth=2,
        )
        self.records_listbox.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.records_listbox.yview)

        bottom = tk.Frame(self.tab_records, bg="#fff", height=40)
        bottom.pack(fill=tk.X)
        bottom.pack_propagate(False)

        ttk.Button(
            bottom,
            text="🔄 刷新记录",
            command=self._on_refresh_records,
        ).pack(side=tk.RIGHT, padx=(0, 5))

        ttk.Button(
            bottom,
            text="🗑️ 一键取消",
            command=self._on_cancel_all_orders,
        ).pack(side=tk.RIGHT, padx=5)

    def _build_profile_tab(self):
        """构建个人信息标签页"""
        info_frame = tk.Frame(self.tab_profile, bg="#fff")
        info_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # 信息卡片
        card = tk.Frame(info_frame, bg="#f0f8f0", relief=tk.GROOVE, borderwidth=2)
        card.pack(fill=tk.X, pady=10, ipady=20)

        tk.Label(
            card,
            text="👤 账户信息",
            bg="#f0f8f0",
            font=("Microsoft YaHei", 14, "bold"),
            fg="#2E8B57",
        ).pack(anchor=tk.W, padx=20, pady=(15, 10))

        self.profile_name = tk.Label(
            card, text="", bg="#f0f8f0", font=("Microsoft YaHei", 12)
        )
        self.profile_name.pack(anchor=tk.W, padx=20, pady=5)

        self.profile_balance = tk.Label(
            card, text="", bg="#f0f8f0", font=("Microsoft YaHei", 12)
        )
        self.profile_balance.pack(anchor=tk.W, padx=20, pady=5)

        self.profile_account = tk.Label(
            card,
            text=f"账号: {USERNAME}",
            bg="#f0f8f0",
            font=("Microsoft YaHei", 12),
        )
        self.profile_account.pack(anchor=tk.W, padx=20, pady=5)

        tk.Label(
            card,
            text=f"学校: 磐安中学",
            bg="#f0f8f0",
            font=("Microsoft YaHei", 12),
        ).pack(anchor=tk.W, padx=20, pady=5)

        ttk.Button(
            info_frame,
            text="🔄 刷新信息",
            command=self._on_refresh_profile,
        ).pack(pady=10)

    # ==================== 自动订餐标签页 ====================

    def _build_auto_order_tab(self):
        """构建自动订餐标签页"""
        # 主容器使用 Canvas 实现滚动
        canvas = tk.Canvas(self.tab_auto, bg="#fff", highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.tab_auto, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg="#fff")

        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor=tk.NW, width=850)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 绑定鼠标滚轮
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        scroll_frame.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>"))

        row = 0

        # ---- 配置区 ----
        cfg_frame = tk.LabelFrame(
            scroll_frame, text="⚙️ 订餐设置", font=("Microsoft YaHei", 11, "bold"),
            bg="#fff", padx=15, pady=10
        )
        cfg_frame.grid(row=row, column=0, sticky=tk.EW, padx=10, pady=5)
        row += 1

        tk.Label(cfg_frame, text="爱吃的关键词:", bg="#fff",
                 font=("Microsoft YaHei", 10)).grid(row=0, column=0, sticky=tk.W, pady=5)
        self.auto_keywords_var = tk.StringVar(
            value=", ".join(self.auto_engine.config.get("keywords", []))
        )
        tk.Entry(cfg_frame, textvariable=self.auto_keywords_var,
                 width=50, font=("Microsoft YaHei", 10)).grid(row=0, column=1, pady=5, padx=5)

        tk.Label(cfg_frame, text="不吃的食材:", bg="#fff",
                 font=("Microsoft YaHei", 10)).grid(row=1, column=0, sticky=tk.W, pady=5)
        self.auto_exclude_var = tk.StringVar(
            value=", ".join(self.auto_engine.config.get("exclude_keywords", []))
        )
        tk.Entry(cfg_frame, textvariable=self.auto_exclude_var,
                 width=50, font=("Microsoft YaHei", 10)).grid(row=1, column=1, pady=5, padx=5)

        # --- 按餐次志愿优先级 ---
        tk.Label(cfg_frame, text="", bg="#fff").grid(row=2, column=0, pady=2)
        tk.Label(cfg_frame, text="① 第一志愿  ② 第二志愿  ③ 第三志愿",
                 bg="#f0f8f0", font=("Microsoft YaHei", 9, "bold"), fg="#555"
                 ).grid(row=2, column=1, sticky=tk.W, padx=5, pady=2)

        for m_idx, (m_name, m_prefix, m_color) in enumerate([
            ("🌅 早餐", "breakfast", "#FF8C00"),
            ("☀️ 午餐", "lunch", "#2E8B57"),
            ("🌙 晚餐", "dinner", "#4169E1"),
        ]):
            r = 3 + m_idx
            tk.Label(cfg_frame, text=m_name, bg="#fff",
                     font=("Microsoft YaHei", 10, "bold"), fg=m_color
                     ).grid(row=r, column=0, sticky=tk.W, pady=2)

            # 横向排列三个志愿输入框
            pri_frame = tk.Frame(cfg_frame, bg="#fff")
            pri_frame.grid(row=r, column=1, sticky=tk.W, padx=5, pady=2)

            for p_idx, (p_name, p_key) in enumerate([
                ("①", f"{m_prefix}_first"),
                ("②", f"{m_prefix}_second"),
                ("③", f"{m_prefix}_third"),
            ]):
                # 每个志愿 3 个独立输入框（对应 3 个空位）
                values = self.auto_engine.config.get(p_key, [])
                for slot_i in range(3):
                    if slot_i == 0:
                        lbl = tk.Label(pri_frame, text=p_name, bg="#fff",
                                       font=("Microsoft YaHei", 8), fg="#888")
                        lbl.pack(side=tk.LEFT, padx=(0, 2))
                    val = values[slot_i] if slot_i < len(values) else ""
                    var = tk.StringVar(value=val)
                    setattr(self, f"auto_{p_key}_var_{slot_i}", var)
                    tk.Entry(pri_frame, textvariable=var,
                             width=8, font=("Microsoft YaHei", 9)
                             ).pack(side=tk.LEFT, padx=(0, 3))
                # 志愿之间留空
                tk.Label(pri_frame, text="    ", bg="#fff",
                         ).pack(side=tk.LEFT)

        tk.Label(cfg_frame, text="检查间隔:", bg="#fff",
                 font=("Microsoft YaHei", 10)).grid(row=6, column=0, sticky=tk.W, pady=5)
        self.auto_interval_var = tk.StringVar(
            value=str(self.auto_engine.config.get("interval_minutes", 30))
        )
        interval_combo = ttk.Combobox(
            cfg_frame, textvariable=self.auto_interval_var,
            values=["15", "30", "60", "120"], width=10, state="readonly",
            font=("Microsoft YaHei", 10)
        )
        interval_combo.grid(row=6, column=1, sticky=tk.W, pady=5, padx=5)
        tk.Label(cfg_frame, text="分钟", bg="#fff",
                 font=("Microsoft YaHei", 10)).grid(row=6, column=1, pady=5, padx=(60, 0))

        tk.Label(cfg_frame, text="每日最多订:", bg="#fff",
                 font=("Microsoft YaHei", 10)).grid(row=7, column=0, sticky=tk.W, pady=5)
        self.auto_max_var = tk.StringVar(
            value=str(self.auto_engine.config.get("max_meals_per_day", 6))
        )
        max_combo = ttk.Combobox(
            cfg_frame, textvariable=self.auto_max_var,
            values=["3", "6", "9", "12", "99"], width=10, state="readonly",
            font=("Microsoft YaHei", 10)
        )
        max_combo.grid(row=7, column=1, sticky=tk.W, pady=5, padx=5)
        tk.Label(cfg_frame, text="餐", bg="#fff",
                 font=("Microsoft YaHei", 10)).grid(row=7, column=1, pady=5, padx=(60, 0))

        # ---- AI 配置区 ----
        ai_frame = tk.LabelFrame(
            scroll_frame, text="🤖 AI 智能推荐 (可选)", font=("Microsoft YaHei", 11, "bold"),
            bg="#fff", padx=15, pady=10
        )
        ai_frame.grid(row=row, column=0, sticky=tk.EW, padx=10, pady=5)
        row += 1

        self.auto_ai_var = tk.BooleanVar(
            value=self.auto_engine.config.get("ai_enabled", False)
        )
        tk.Checkbutton(
            ai_frame, text="启用 AI 智能推荐", variable=self.auto_ai_var,
            bg="#fff", font=("Microsoft YaHei", 10)
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=5)

        tk.Label(ai_frame, text="API Key:", bg="#fff",
                 font=("Microsoft YaHei", 10)).grid(row=1, column=0, sticky=tk.W, pady=5)
        self.auto_ai_key_var = tk.StringVar(
            value=self.auto_engine.config.get("ai_api_key", "")
        )
        ai_key_entry = tk.Entry(
            ai_frame, textvariable=self.auto_ai_key_var,
            width=50, font=("Microsoft YaHei", 10), show="*"
        )
        ai_key_entry.grid(row=1, column=1, pady=5, padx=5)

        tk.Label(ai_frame, text="API 类型:", bg="#fff",
                 font=("Microsoft YaHei", 10)).grid(row=2, column=0, sticky=tk.W, pady=5)
        self.auto_ai_type_var = tk.StringVar(
            value=self.auto_engine.config.get("ai_api_type", "openai")
        )
        ttk.Combobox(
            ai_frame, textvariable=self.auto_ai_type_var,
            values=["openai (DeepSeek / 兼容)", "anthropic (Claude)"],
            width=30, state="readonly", font=("Microsoft YaHei", 10)
        ).grid(row=2, column=1, sticky=tk.W, pady=5, padx=5)

        tk.Label(ai_frame, text="模型:", bg="#fff",
                 font=("Microsoft YaHei", 10)).grid(row=3, column=0, sticky=tk.W, pady=5)
        self.auto_ai_model_var = tk.StringVar(
            value=self.auto_engine.config.get("ai_model", "deepseek-v4-flash")
        )
        ttk.Combobox(
            ai_frame, textvariable=self.auto_ai_model_var,
            values=[
                "deepseek-v4-flash",
                "deepseek-chat",
                "deepseek-reasoner",
                "claude-haiku-4-5",
                "claude-sonnet-4-6",
                "claude-opus-4-8",
            ],
            width=30, state="editable", font=("Microsoft YaHei", 10)
        ).grid(row=3, column=1, sticky=tk.W, pady=5, padx=5)

        tk.Label(ai_frame, text="API 地址:", bg="#fff",
                 font=("Microsoft YaHei", 10)).grid(row=4, column=0, sticky=tk.W, pady=5)
        self.auto_ai_url_var = tk.StringVar(
            value=self.auto_engine.config.get("ai_api_url", "https://api.deepseek.com/v1")
        )
        tk.Entry(ai_frame, textvariable=self.auto_ai_url_var,
                 width=50, font=("Microsoft YaHei", 10)
        ).grid(row=4, column=1, pady=5, padx=5)

        # 使用说明
        tk.Label(
            ai_frame,
            text="💡 DeepSeek: api.deepseek.com  |  Anthropic: api.anthropic.com",
            fg="#888", bg="#fff", font=("Microsoft YaHei", 8)
        ).grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=2)

        # ---- 控制区 ----
        ctrl_frame = tk.Frame(scroll_frame, bg="#fff")
        ctrl_frame.grid(row=row, column=0, sticky=tk.EW, padx=10, pady=8)
        row += 1

        self.auto_start_btn = ttk.Button(
            ctrl_frame, text="▶ 启动自动订餐",
            command=self._on_auto_start, style="Accent.TButton"
        )
        self.auto_start_btn.pack(side=tk.LEFT, padx=5)

        self.auto_stop_btn = ttk.Button(
            ctrl_frame, text="⏹ 停止",
            command=self._on_auto_stop, style="Danger.TButton"
        )
        self.auto_stop_btn.pack(side=tk.LEFT, padx=5)
        self.auto_stop_btn.config(state=tk.DISABLED)

        self.auto_once_btn = ttk.Button(
            ctrl_frame, text="▶ 立即执行一次",
            command=self._on_auto_run_once
        )
        self.auto_once_btn.pack(side=tk.LEFT, padx=5)

        self.auto_save_btn = ttk.Button(
            ctrl_frame, text="💾 保存设置",
            command=self._on_auto_save
        )
        self.auto_save_btn.pack(side=tk.LEFT, padx=5)

        self.auto_sync_btn = ttk.Button(
            ctrl_frame, text="☁️ 同步到 GitHub",
            command=self._on_sync_to_github
        )
        self.auto_sync_btn.pack(side=tk.LEFT, padx=5)

        # ---- 状态显示 ----
        status_frame = tk.Frame(scroll_frame, bg="#fff")
        status_frame.grid(row=row, column=0, sticky=tk.EW, padx=10, pady=5)
        row += 1

        tk.Label(status_frame, text="状态:", bg="#fff",
                 font=("Microsoft YaHei", 10, "bold")).pack(side=tk.LEFT)
        self.auto_status_var = tk.StringVar(value="● 已停止")
        self.auto_status_label = tk.Label(
            status_frame, textvariable=self.auto_status_var,
            fg="#999", bg="#fff", font=("Microsoft YaHei", 10)
        )
        self.auto_status_label.pack(side=tk.LEFT, padx=5)

        tk.Label(status_frame, text="  下次检查:", bg="#fff",
                 font=("Microsoft YaHei", 10)).pack(side=tk.LEFT, padx=(20, 0))
        self.auto_next_var = tk.StringVar(value="--")
        tk.Label(status_frame, textvariable=self.auto_next_var,
                 bg="#fff", font=("Microsoft YaHei", 10)).pack(side=tk.LEFT, padx=5)

        # ---- 日志区 ----
        log_frame = tk.LabelFrame(
            scroll_frame, text="📋 操作日志", font=("Microsoft YaHei", 11, "bold"),
            bg="#fff", padx=10, pady=5
        )
        log_frame.grid(row=row, column=0, sticky=tk.EW, padx=10, pady=5)
        row += 1

        self.auto_log_text = scrolledtext.ScrolledText(
            log_frame, width=90, height=16,
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            relief=tk.FLAT, borderwidth=2
        )
        self.auto_log_text.pack(padx=5, pady=5)

        # 显示已有日志
        for entry in self.auto_engine.logger.get_all():
            self.auto_log_text.insert(tk.END, entry + "\n")
        self.auto_log_text.see(tk.END)

    # ---- 自动订餐事件处理 ----

    def _on_auto_save(self):
        """保存自动订餐设置"""
        keywords = [k.strip() for k in self.auto_keywords_var.get().split(",") if k.strip()]
        exclude_kw = [k.strip() for k in self.auto_exclude_var.get().split(",") if k.strip()]

        # 解析 API 类型
        ai_type_raw = self.auto_ai_type_var.get()
        if "anthropic" in ai_type_raw:
            ai_type = "anthropic"
        else:
            ai_type = "openai"

        # 按餐次志愿关键词（每个志愿3个独立输入框）
        kw_updates = {}
        for prefix in ["breakfast", "lunch", "dinner"]:
            for level in ["first", "second", "third"]:
                key = f"{prefix}_{level}"
                values = []
                for slot_i in range(3):
                    var = getattr(self, f"auto_{key}_var_{slot_i}", None)
                    if var:
                        v = var.get().strip()
                        if v:
                            values.append(v)
                kw_updates[key] = values

        self.auto_engine.update_config(
            keywords=keywords,
            exclude_keywords=exclude_kw,
            interval_minutes=int(self.auto_interval_var.get()),
            max_meals_per_day=int(self.auto_max_var.get()),
            ai_enabled=self.auto_ai_var.get(),
            ai_api_key=self.auto_ai_key_var.get(),
            ai_model=self.auto_ai_model_var.get(),
            ai_api_type=ai_type,
            ai_api_url=self.auto_ai_url_var.get().rstrip("/"),
            **kw_updates,
        )
        messagebox.showinfo("保存成功", "自动订餐设置已保存")
        self.footer_label.config(text="设置已保存")

    def _on_sync_to_github(self):
        """同步当前账号的关键词配置到 GitHub Secrets"""
        # 先保存
        self._on_auto_save()

        account = self._accounts[self._active_idx]
        repo = account.get("github_repo", "")
        if not repo:
            messagebox.showwarning("无法同步", "当前账号未关联 GitHub 仓库")
            return

        if not messagebox.askyesno(
            "确认同步",
            f"将「{account['name']}」的订餐关键词同步到\n"
            f"GitHub 仓库 {repo} 吗？\n\n"
            f"同步后 GitHub Actions 将使用新的配置。"
        ):
            return

        self.footer_label.config(text=f"正在同步到 GitHub...")

        def _do_sync():
            try:
                # 读取 GitHub token
                token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
                if not token:
                    token_file = os.path.join(os.path.dirname(__file__), ".github_token")
                    try:
                        with open(token_file, "r") as f:
                            token = f.read().strip()
                    except FileNotFoundError:
                        self.root.after(0, lambda: messagebox.showerror(
                            "缺少 Token",
                            "未找到 GitHub Token。\n"
                            "请在项目目录创建 .github_token 文件，\n"
                            "内容为你的 GitHub Personal Access Token。"
                        ))
                        return

                from nacl import encoding, public

                def encrypt_secret(pubkey_pem, val):
                    pk = public.PublicKey(pubkey_pem.encode(), encoding.Base64Encoder())
                    return base64.b64encode(
                        public.SealedBox(pk).encrypt(val.encode())
                    ).decode()

                def gh(path, data=None):
                    url = f"https://api.github.com{path}"
                    body = json.dumps(data).encode() if data else None
                    req = urllib.request.Request(url, data=body, method="PUT" if data else "GET")
                    req.add_header("Authorization", f"Bearer {token}")
                    req.add_header("Accept", "application/vnd.github+json")
                    if body:
                        req.add_header("Content-Type", "application/json")
                    resp = urllib.request.urlopen(req, timeout=30)
                    result = resp.read().decode()
                    return json.loads(result) if result.strip() else {}

                # 获取 repo 的公钥
                owner_repo = repo
                pk = gh(f"/repos/{owner_repo}/actions/secrets/public-key")
                key_id = pk["key_id"]

                def set_secret(name, value):
                    gh(f"/repos/{owner_repo}/actions/secrets/{name}", {
                        "encrypted_value": encrypt_secret(pk["key"], value),
                        "key_id": key_id,
                    })

                cfg = self.auto_engine.config
                secrets = {
                    "KEYWORDS": ",".join(cfg.get("keywords", [])),
                    "EXCLUDE_KEYWORDS": ",".join(cfg.get("exclude_keywords", [])),
                    "BREAKFAST_FIRST": ",".join(cfg.get("breakfast_first", [])),
                    "BREAKFAST_SECOND": ",".join(cfg.get("breakfast_second", [])),
                    "BREAKFAST_THIRD": ",".join(cfg.get("breakfast_third", [])),
                    "LUNCH_FIRST": ",".join(cfg.get("lunch_first", [])),
                    "LUNCH_SECOND": ",".join(cfg.get("lunch_second", [])),
                    "LUNCH_THIRD": ",".join(cfg.get("lunch_third", [])),
                    "DINNER_FIRST": ",".join(cfg.get("dinner_first", [])),
                    "DINNER_SECOND": ",".join(cfg.get("dinner_second", [])),
                    "DINNER_THIRD": ",".join(cfg.get("dinner_third", [])),
                    "AI_API_KEY": cfg.get("ai_api_key", ""),
                }

                ok = 0
                for name, value in secrets.items():
                    try:
                        set_secret(name, value)
                        ok += 1
                    except Exception as e:
                        print(f"  FAIL {name}: {e}")

                self.root.after(0, lambda: [
                    messagebox.showinfo("同步完成",
                        f"✅ 已同步 {ok}/{len(secrets)} 个配置到\n{repo}"),
                    self.footer_label.config(text=f"☁️ 已同步到 GitHub ({ok}项)")
                ])
            except Exception as e:
                self.root.after(0, lambda: [
                    messagebox.showerror("同步失败", str(e)),
                    self.footer_label.config(text=f"同步失败: {e}")
                ])

        threading.Thread(target=_do_sync, daemon=True).start()

    def _auto_log_callback(self, msg):
        """日志回调（从工作线程回到 GUI 线程）"""
        self.root.after(0, lambda: self._append_auto_log(msg))

    def _append_auto_log(self, msg):
        """追加日志到文本框"""
        self.auto_log_text.insert(tk.END, msg + "\n")
        self.auto_log_text.see(tk.END)

    def _on_auto_start(self):
        """启动自动订餐"""
        # 先保存设置
        self._on_auto_save()

        # 更新 UI 状态
        self.auto_start_btn.config(state=tk.DISABLED)
        self.auto_stop_btn.config(state=tk.NORMAL)
        self.auto_once_btn.config(state=tk.DISABLED)
        self.auto_status_var.set("● 运行中")
        self.auto_status_label.config(fg="#2E8B57")

        # 更新下次检查时间
        interval = self.auto_engine.config.get("interval_minutes", 30)
        next_time = (datetime.now() + timedelta(minutes=interval)).strftime("%H:%M")
        self.auto_next_var.set(next_time)

        # 启动引擎
        self.auto_engine.start(on_log=self._auto_log_callback)
        self.footer_label.config(text="自动订餐已启动")

    def _on_auto_stop(self):
        """停止自动订餐"""
        self.auto_engine.stop(on_log=self._auto_log_callback)

        self.auto_start_btn.config(state=tk.NORMAL)
        self.auto_stop_btn.config(state=tk.DISABLED)
        self.auto_once_btn.config(state=tk.NORMAL)
        self.auto_status_var.set("● 已停止")
        self.auto_status_label.config(fg="#999")
        self.auto_next_var.set("--")
        self.footer_label.config(text="自动订餐已停止")

    def _on_auto_run_once(self):
        """立即执行一次订餐检查"""
        self._on_auto_save()
        self.auto_once_btn.config(state=tk.DISABLED)
        self.auto_status_var.set("● 执行中...")
        self.auto_status_label.config(fg="#FF8C00")
        self.footer_label.config(text="正在执行自动订餐检查...")

        def done_callback(success, msg):
            self.root.after(0, lambda: self._auto_run_done(success, msg))

        self.auto_engine.run_once(
            on_log=self._auto_log_callback,
            on_done=done_callback,
        )

    def _auto_run_done(self, success, msg):
        """立即执行完成回调"""
        self.auto_once_btn.config(state=tk.NORMAL)
        if self.auto_engine._running:
            self.auto_status_var.set("● 运行中")
            self.auto_status_label.config(fg="#2E8B57")
        else:
            self.auto_status_var.set("● 已停止")
            self.auto_status_label.config(fg="#999")
        self.footer_label.config(text=msg)

    # ==================== AI 订餐助手标签页 ====================

    def _build_chat_tab(self):
        """构建 AI 订餐助手聊天界面"""
        # 聊天记录显示区
        chat_frame = tk.Frame(self.tab_chat, bg="#f5f5f5")
        chat_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.chat_text = tk.Text(
            chat_frame,
            wrap=tk.WORD,
            font=("Microsoft YaHei", 10),
            bg="#fafafa",
            relief=tk.FLAT,
            borderwidth=2,
            state=tk.DISABLED,
        )
        chat_scroll = tk.Scrollbar(chat_frame, command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=chat_scroll.set)

        self.chat_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        chat_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 欢迎消息
        self._append_chat("小食", "你好呀！我是食堂订餐助手「小食」🤖\n\n"
                           "你可以这样跟我说话：\n"
                           "• 「明天午餐有什么？」 — 查看菜单\n"
                           "• 「帮我订套餐C」 — 加购套餐\n"
                           "• 「帮我下单」 — 提交订单\n"
                           "• 「看看购物车」 — 查看已选\n\n"
                           "想吃什么尽管跟我说！😊")

        # 输入区
        input_frame = tk.Frame(self.tab_chat, bg="#fff", height=55)
        input_frame.pack(fill=tk.X, side=tk.BOTTOM)
        input_frame.pack_propagate(False)

        self.chat_input = tk.Entry(
            input_frame,
            font=("Microsoft YaHei", 11),
            relief=tk.GROOVE,
            borderwidth=2,
        )
        self.chat_input.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8, pady=8)
        self.chat_input.bind("<Return>", lambda e: self._on_chat_send())

        self.chat_send_btn = ttk.Button(
            input_frame,
            text="发送 💬",
            command=self._on_chat_send,
            style="Accent.TButton",
        )
        self.chat_send_btn.pack(side=tk.RIGHT, padx=8, pady=8)

        # 快捷按钮
        quick_frame = tk.Frame(self.tab_chat, bg="#fff", height=35)
        quick_frame.pack(fill=tk.X, side=tk.BOTTOM)
        quick_frame.pack_propagate(False)

        for text, msg in [
            ("📋 午餐菜单", "今天午餐有什么？"),
            ("🛒 购物车", "看看购物车"),
            ("✅ 提交订单", "帮我下单"),
            ("📝 订餐记录", "我订过什么"),
            ("💰 余额", "我还有多少钱"),
            ("🧠 我的记忆", "你记得关于我的什么？"),
        ]:
            tk.Button(
                quick_frame,
                text=text,
                font=("Microsoft YaHei", 9),
                bg="#f0f8f0",
                relief=tk.FLAT,
                cursor="hand2",
                command=lambda m=msg: self._quick_chat(m),
            ).pack(side=tk.LEFT, padx=3, pady=3)

    def _append_chat(self, sender, message):
        """在聊天框追加消息"""
        self.chat_text.configure(state=tk.NORMAL)

        # 消息分隔
        self.chat_text.insert(tk.END, "\n")

        if sender == "我":
            self.chat_text.insert(tk.END, f"🙋 {sender}：\n")
            self.chat_text.insert(tk.END, f"{message}\n")
        elif sender == "小食":
            self.chat_text.insert(tk.END, f"🤖 {sender}：\n")
            # 用 tag 给消息内容上色
            tag_name = f"msg_{self.chat_text.index(tk.END)}"
            self.chat_text.insert(tk.END, f"{message}\n", tag_name)
            self.chat_text.tag_config(tag_name, foreground="#2E8B57")

        self.chat_text.see(tk.END)
        self.chat_text.configure(state=tk.DISABLED)

    def _quick_chat(self, message):
        """快捷消息"""
        self.chat_input.delete(0, tk.END)
        self.chat_input.insert(0, message)
        self._on_chat_send()

    def _on_chat_send(self):
        """发送聊天消息"""
        user_msg = self.chat_input.get().strip()
        if not user_msg:
            return

        self.chat_input.delete(0, tk.END)
        self._append_chat("我", user_msg)
        self.chat_send_btn.config(state=tk.DISABLED)
        self.footer_label.config(text="AI 思考中...")

        def chat_thread():
            try:
                reply, action = self.chat_assistant.chat(user_msg)
                self.root.after(0, lambda: self._on_chat_result(reply))
            except Exception as e:
                self.root.after(
                    0, lambda: self._append_chat("小食", f"😅 出错了：{e}")
                )
            finally:
                self.root.after(0, lambda: self.chat_send_btn.config(state=tk.NORMAL))
                self.root.after(0, lambda: self.footer_label.config(text="就绪"))

        threading.Thread(target=chat_thread, daemon=True).start()

    def _on_chat_result(self, reply):
        """聊天结果"""
        self._append_chat("小食", reply)

    # ==================== 事件处理 ====================

    def _do_login(self):
        """执行登录"""

        def login_thread():
            success, msg = self.client.login()
            self.root.after(0, lambda: self._on_login_result(success, msg))

        self.footer_label.config(text="正在登录...")
        threading.Thread(target=login_thread, daemon=True).start()

    def _on_login_result(self, success, msg):
        if success:
            account = self._accounts[self._active_idx]
            self.status_label.config(
                text=f"✅ {account['name']}", fg="white"
            )
            self.footer_label.config(text="登录成功 - " + account["name"])
            # 获取用户信息
            info = self.client.get_user_info()
            if info:
                name = info.get("name", "")
                balance = info.get("balance", "?")
                self.user_label.config(text=f"👤 {name}")
                self.balance_label.config(text=f"💰 余额: ¥{balance}")
                self.profile_name.config(text=f"姓名: {name}")
                self.profile_balance.config(text=f"余额: ¥{balance}")
            # 加载菜单
            self._on_query_menu()
            self._on_refresh_cart()
        else:
            self.status_label.config(text="❌ 登录失败", fg="white")
            self.footer_label.config(text=msg)
            messagebox.showerror("登录失败", msg)

    def _on_query_menu(self):
        """查询菜单"""

        def query_thread():
            date_str = self.date_var.get()
            meal_name = self.meal_var.get()
            meal_code = {"早餐": 1, "午餐": 2, "晚餐": 3}.get(meal_name, 2)

            try:
                items = self.client.get_menu(date_str, meal_code)
                self.menu_items = items
                self.current_date = date_str
                self.current_meal = meal_code

                self.root.after(0, lambda: self._display_menu(items, meal_name))
            except Exception as e:
                self.root.after(
                    0, lambda: self.footer_label.config(text=f"查询失败: {e}")
                )

        self.footer_label.config(text="正在查询菜单...")
        threading.Thread(target=query_thread, daemon=True).start()

    def _display_menu(self, items, meal_name):
        """显示菜单"""
        self.menu_listbox.delete(0, tk.END)

        if not items:
            self.menu_listbox.insert(tk.END, f"{'='*50}")
            self.menu_listbox.insert(tk.END, f"   {self.current_date} {meal_name} - 暂无可用套餐")
            self.menu_listbox.insert(tk.END, f"{'='*50}")
            self.menu_info_label.config(text="暂无菜单")
            self.footer_label.config(text=f"{self.current_date} {meal_name} 无菜单")
            return

        # 存储每个商品对应的 listbox 行范围 (start_line, end_line_exclusive)
        self._item_line_ranges = []

        header = f"{'='*60}"
        self.menu_listbox.insert(tk.END, header)
        self.menu_listbox.insert(
            tk.END, f"   📅 {self.current_date}  {meal_name}  共 {len(items)} 种套餐"
        )
        self.menu_listbox.insert(tk.END, header)

        for i, item in enumerate(items):
            start = self.menu_listbox.size()
            self.menu_listbox.insert(tk.END, "")
            self.menu_listbox.insert(
                tk.END, f"  🔸 {item['name']}        ¥{item['price']}"
            )
            self.menu_listbox.insert(tk.END, f"     {item['description']}")
            self.menu_listbox.insert(
                tk.END,
                f"     已售 {item['sold']}/{item['total']}  剩余 {item['available']}份  [编号: {item['setmno']}]",
            )
            self.menu_listbox.insert(tk.END, f"  {'─'*55}")
            end = self.menu_listbox.size()
            self._item_line_ranges.append((start, end, i))

        self.menu_info_label.config(
            text=f"共 {len(items)} 种套餐 | 选择后点击「加入购物车」"
        )
        self.footer_label.config(text=f"已加载 {self.current_date} {meal_name} 菜单")

    def _on_add_to_cart(self):
        """加入购物车"""
        selection = self.menu_listbox.curselection()
        if not selection:
            messagebox.showwarning("提示", "请先在菜单中选择一个套餐")
            return

        # 根据选中的行号找到对应的商品索引
        selected_idx = selection[0]
        item_idx = -1
        for start, end, idx in self._item_line_ranges:
            if start <= selected_idx < end:
                item_idx = idx
                break

        if item_idx < 0 or item_idx >= len(self.menu_items):
            messagebox.showerror("错误", "请重新选择")
            return

        item = self.menu_items[item_idx]

        def add_thread():
            try:
                result = self.client.add_to_cart(item["setmno"], self.current_date, self.current_meal)
                if result.get("flag"):
                    self.root.after(
                        0,
                        lambda: (
                            self.order_status_label.config(
                                text=f"✅ {item['name']} 已加入购物车", fg="#2E8B57"
                            ),
                            self.footer_label.config(text=f"已添加 {item['name']}"),
                            self._on_refresh_cart(),
                        ),
                    )
                else:
                    self.root.after(
                        0,
                        lambda: (
                            self.order_status_label.config(
                                text=f"❌ {result.get('msg', '添加失败')}", fg="red"
                            ),
                        ),
                    )
            except Exception as e:
                self.root.after(
                    0, lambda: self.footer_label.config(text=f"添加失败: {e}")
                )

        threading.Thread(target=add_thread, daemon=True).start()

    def _on_refresh_cart(self):
        """刷新购物车"""

        def refresh_thread():
            try:
                items, total, count, html = self.client.get_cart()
                self.cart_items = items
                self.cart_total = total
                self.cart_count = count

                self.root.after(0, self._display_cart)
            except Exception as e:
                self.root.after(
                    0, lambda: self.footer_label.config(text=f"刷新购物车失败: {e}")
                )

        threading.Thread(target=refresh_thread, daemon=True).start()

    def _display_cart(self):
        """显示购物车"""
        self.cart_listbox.delete(0, tk.END)

        if not self.cart_items:
            self.cart_listbox.insert(tk.END, "  🛒 购物车为空")
            self.cart_listbox.insert(tk.END, "  请先在「订餐」页面选择套餐加入购物车")
            self.cart_info_label.config(text="购物车为空", fg="#E74C3C")
            self.footer_label.config(text="购物车为空")
            return

        self.cart_listbox.insert(tk.END, f"{'='*55}")
        self.cart_listbox.insert(
            tk.END, f"  购物车  共 {self.cart_count} 件  总计: ¥{self.cart_total}"
        )
        self.cart_listbox.insert(tk.END, f"{'='*55}")

        for item in self.cart_items:
            meal_name = MEAL_NAMES.get(item["meal_code"], f"餐次{item['meal_code']}")
            self.cart_listbox.insert(tk.END, "")
            self.cart_listbox.insert(
                tk.END,
                f"  📦 套餐编号: {item['setmno']}",
            )
            self.cart_listbox.insert(
                tk.END, f"     {item['date_str']} {meal_name} × {item['quantity']}份"
            )
            self.cart_listbox.insert(tk.END, f"  {'─'*50}")

        self.cart_info_label.config(
            text=f"共 {self.cart_count} 件商品 | 总计: ¥{self.cart_total}",
            fg="#2E8B57",
        )
        self.footer_label.config(text=f"购物车: {self.cart_count} 件")

    def _on_delete_from_cart(self):
        """从购物车删除"""
        selection = self.cart_listbox.curselection()
        if not selection:
            messagebox.showwarning("提示", "请先在购物车中选择要删除的商品")
            return

        if not self.cart_items:
            return

        # 简单处理：删除第一个商品
        item = self.cart_items[0]

        if not messagebox.askyesno("确认", "确定要删除此商品吗？"):
            return

        def delete_thread():
            try:
                result = self.client.delete_from_cart(
                    item["setmno"], item["date_str"], item["meal_code"]
                )
                if result.get("flag"):
                    self.root.after(
                        0,
                        lambda: (
                            self.footer_label.config(text="已从购物车删除"),
                            self._on_refresh_cart(),
                        ),
                    )
                else:
                    self.root.after(
                        0,
                        lambda: self.footer_label.config(
                            text=f"删除失败: {result.get('msg', '')}"
                        ),
                    )
            except Exception as e:
                self.root.after(
                    0, lambda: self.footer_label.config(text=f"删除失败: {e}")
                )

        threading.Thread(target=delete_thread, daemon=True).start()

    def _on_submit_order(self):
        """提交订单"""
        if not self.cart_items:
            messagebox.showwarning("提示", "购物车为空，请先选餐")
            return

        if not messagebox.askyesno(
            "确认下单",
            f"购物车共 {self.cart_count} 件商品\n总计: ¥{self.cart_total}\n\n确认提交订单吗？"
            + "\n(将从主账户余额扣款)",
        ):
            return

        def submit_thread():
            try:
                result = self.client.submit_order()
                if result.get("flag"):
                    msg = result.get("msg", "")
                    ccnum = result.get("ccnum", 0)
                    tcnum = result.get("tcnum", 0)
                    ddnum = result.get("ddnum", 0)

                    if ccnum and tcnum and ccnum == tcnum:
                        tip = f"下单成功！已订 {ccnum} 个餐次各1份套餐"
                    elif ccnum and tcnum:
                        tip = f"下单成功！已订 {ccnum} 个餐次共 {tcnum} 份套餐"
                    else:
                        tip = "下单成功！"

                    self.root.after(
                        0,
                        lambda: (
                            messagebox.showinfo("成功", tip),
                            self._on_refresh_cart(),
                            self._on_refresh_settle(),
                            self._on_refresh_profile(),
                        ),
                    )
                    self.root.after(
                        0, lambda: self.footer_label.config(text="下单成功")
                    )
                else:
                    err = result.get("mng", result.get("msg", "下单失败"))
                    self.root.after(
                        0,
                        lambda: (
                            messagebox.showerror("失败", f"下单失败: {err}"),
                            self.footer_label.config(text=f"下单失败: {err}"),
                        ),
                    )
            except Exception as e:
                self.root.after(
                    0,
                    lambda: (
                        messagebox.showerror("错误", f"下单异常: {e}"),
                        self.footer_label.config(text=f"下单异常: {e}"),
                    ),
                )

        threading.Thread(target=submit_thread, daemon=True).start()

    def _on_refresh_settle(self):
        """刷新结算统计"""

        def refresh_thread():
            try:
                summary, total = self.client.get_settlement_summary()
                self.root.after(0, lambda: self._display_settle(summary, total))
            except Exception as e:
                self.root.after(
                    0, lambda: self.footer_label.config(text=f"刷新统计失败: {e}")
                )

        threading.Thread(target=refresh_thread, daemon=True).start()

    def _display_settle(self, summary, total):
        """显示结算统计"""
        for item in self.settle_tree.get_children():
            self.settle_tree.delete(item)

        if not summary:
            self.settle_total_label.config(text="暂无统计数据")
            return

        for row in summary:
            meals = row["meals"]
            values = [f"{row['weekday']} {row['date']}"]
            for i in range(3):
                status = meals[i] if i < len(meals) else "未订"
                values.append(status)
            self.settle_tree.insert("", tk.END, values=values)

        self.settle_total_label.config(text=f"总计: ¥{total}")

    def _on_cancel_all_orders(self):
        """一键取消所有已订餐"""
        if not messagebox.askyesno(
            "确认取消",
            "确定要取消所有已订餐吗？\n\n取消后金额将退回余额。",
        ):
            return

        self.footer_label.config(text="正在取消所有订餐...")

        def cancel_thread():
            logs = []
            def _log(msg):
                logs.append(msg)
                self.root.after(0, lambda m=msg: self.footer_label.config(text=m))

            try:
                cancelled, failed = self.client.cancel_all_orders(on_log=_log)
                self.root.after(0, lambda: self._on_refresh_settle())
                self.root.after(0, lambda: self._on_refresh_records())
                msg = f"✅ 取消完成：{len(cancelled)} 餐成功"
                if failed:
                    msg += f"，{len(failed)} 餐失败"
                self.root.after(0, lambda m=msg: self.footer_label.config(text=m))
                detail = "\n".join(cancelled[:10])
                if failed:
                    detail += "\n\n❌ 失败:\n" + "\n".join(failed[:5])
                self.root.after(
                    0,
                    lambda: messagebox.showinfo("取消结果", msg + "\n\n" + detail),
                )
            except Exception as e:
                self.root.after(
                    0,
                    lambda: self.footer_label.config(text=f"取消失败: {e}"),
                )

        threading.Thread(target=cancel_thread, daemon=True).start()

    def _on_refresh_records(self):
        """刷新订餐记录"""

        def refresh_thread():
            try:
                orders = self.client.get_order_records()
                self.root.after(0, lambda: self._display_records(orders))
            except Exception as e:
                self.root.after(
                    0, lambda: self.footer_label.config(text=f"刷新记录失败: {e}")
                )

        threading.Thread(target=refresh_thread, daemon=True).start()

    def _display_records(self, orders):
        """显示订餐记录"""
        self.records_listbox.delete(0, tk.END)

        if not orders:
            self.records_listbox.insert(tk.END, "暂无订餐记录")
            return

        self.records_listbox.insert(tk.END, f"{'='*55}")
        self.records_listbox.insert(tk.END, f"  订餐记录  共 {len(orders)} 条")
        self.records_listbox.insert(tk.END, f"{'='*55}")

        total = 0
        for order in orders:
            total += order["price"]
            mc = order.get("meal_code", 0)
            meal_tag = f" ({MEAL_NAMES.get(mc, '?')})" if mc else ""
            self.records_listbox.insert(tk.END, "")
            self.records_listbox.insert(
                tk.END,
                f"  📅 {order['date']}{meal_tag}   {order['name']}   ¥{order['price']}",
            )
            self.records_listbox.insert(tk.END, f"  {'─'*50}")

        self.records_listbox.insert(tk.END, "")
        self.records_listbox.insert(
            tk.END, f"  💰 总计消费: ¥{total}"
        )

    def _on_refresh_profile(self):
        """刷新个人信息"""

        def refresh_thread():
            try:
                info = self.client.get_user_info()
                if info:
                    self.root.after(
                        0,
                        lambda: (
                            self.profile_name.config(
                                text=f"姓名: {info.get('name', '')}"
                            ),
                            self.profile_balance.config(
                                text=f"余额: ¥{info.get('balance', '?')}"
                            ),
                            self.balance_label.config(
                                text=f"💰 余额: ¥{info.get('balance', '?')}"
                            ),
                        ),
                    )
            except Exception as e:
                self.root.after(
                    0, lambda: self.footer_label.config(text=f"刷新信息失败: {e}")
                )

        threading.Thread(target=refresh_thread, daemon=True).start()


# ==================== 主程序 ====================
def main():
    root = tk.Tk()
    app = CanteenApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
