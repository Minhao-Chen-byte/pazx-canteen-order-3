#!/usr/bin/env python3
"""GitHub Actions 专用：从环境变量生成 auto_order_config.json"""
import json, os

def split_env(name, default=""):
    val = os.environ.get(name, default)
    if not val or not val.strip():
        return []
    return [k.strip() for k in val.split(",") if k.strip()]

config = {
    "enabled": True,
    "keywords": split_env("CFG_KEYWORDS"),
    "exclude_keywords": split_env("CFG_EXCLUDE"),
    "breakfast_keywords": [],
    "lunch_keywords": [],
    "dinner_keywords": [],
    "breakfast_first":  split_env("CFG_BF_FIRST"),
    "breakfast_second": split_env("CFG_BF_SECOND"),
    "breakfast_third":  split_env("CFG_BF_THIRD"),
    "lunch_first":  split_env("CFG_LU_FIRST"),
    "lunch_second": split_env("CFG_LU_SECOND"),
    "lunch_third":  split_env("CFG_LU_THIRD"),
    "dinner_first":  split_env("CFG_DN_FIRST"),
    "dinner_second": split_env("CFG_DN_SECOND"),
    "dinner_third":  split_env("CFG_DN_THIRD"),
    "interval_minutes": 120,
    "portions_per_meal": 1,
    "max_meals_per_day": 30,
    "min_balance": 5.0,
    "ai_enabled": True,
    "ai_api_type": "openai",
    "ai_api_key": os.environ.get("CFG_AI_KEY", ""),
    "ai_model": "deepseek-v4-flash",
    "ai_api_url": "https://api.deepseek.com/v1",
}

with open("auto_order_config.json", "w", encoding="utf-8") as f:
    json.dump(config, f, ensure_ascii=False, indent=2)

print("✅ auto_order_config.json 已生成")
for k, v in config.items():
    if k.endswith("_first") or k.endswith("_second") or k.endswith("_third"):
        if v:
            print(f"  {k}: {v}")
