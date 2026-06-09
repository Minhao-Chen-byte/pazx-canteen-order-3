# 磐安中学智慧食堂 - 后台自动订餐设置指南

## 文件说明

| 文件 | 用途 |
|------|------|
| `auto_order_daemon.py` | 后台订餐脚本（无需 GUI） |
| `auto_order_daemon.log` | 运行日志（自动生成） |
| `auto_order_config.json` | 订餐配置（与 GUI 共用） |

## 使用方法

### 方法一：手动运行一次
```bash
cd 程序安装目录
python auto_order_daemon.py
```
执行一次订餐检查后自动退出。

### 方法二：持续监控模式
```bash
python auto_order_daemon.py --loop
```
持续运行，每 30 分钟检查一次（按配置中的间隔）。

### 方法三：Windows 任务计划程序（推荐）

设置后电脑开机自动运行，无需手动启动。

#### 步骤：

1. **打开任务计划程序**
   - 按 `Win + R`，输入 `taskschd.msc`，回车

2. **创建基本任务**
   - 右侧点击「创建基本任务…」
   - 名称：`智慧食堂自动订餐`
   - 描述：自动检查并订餐
   - 下一步

3. **触发器**
   - 选择「每天」→ 下一步
   - 开始时间：`08:00`
   - 每隔：`1 天`
   - 下一步

4. **操作**
   - 选择「启动程序」→ 下一步
   - 程序或脚本：`python`
   - 添加参数：
     ```
     程序安装目录\auto_order_daemon.py
     ```
   - 起始于：
     ```
     程序安装目录
     ```
   - 下一步 → 完成

5. **修改触发器（可选，推荐）**
   - 在任务列表中右键刚创建的任务 →「属性」
   - 「触发器」选项卡 → 编辑
   - 勾选「重复任务间隔」→ 选择 `30 分钟`，持续 `1 天`
   - 这样每天 08:00 启动后，每 30 分钟执行一次

6. **高级设置（可选）**
   - 「条件」选项卡 → 取消勾选「只有在计算机使用交流电源时才启动此任务」
   - 「设置」选项卡 → 勾选「如果任务失败，按以下频率重新启动：每 5 分钟，重新启动次数 3」

#### 或者用命令行直接导入（PowerShell 管理员）：

```powershell
# 每天 08:00-22:00 每 30 分钟执行一次
$action = New-ScheduledTaskAction -Execute "python" -Argument "程序安装目录\auto_order_daemon.py" -WorkingDirectory "程序安装目录"
$trigger = New-ScheduledTaskTrigger -Daily -At "08:00" -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Hours 14)
Register-ScheduledTask -TaskName "智慧食堂自动订餐" -Action $action -Trigger $trigger -RunLevel Highest
```

### 方法四：开机自启动（简单版）

将 `auto_order_daemon.py --loop` 添加到开机启动项：
1. 按 `Win + R`，输入 `shell:startup`，回车
2. 创建一个快捷方式，目标为：
   ```
   python 程序安装目录\auto_order_daemon.py --loop
   ```
3. 每次开机就会自动在后台运行

## 查看日志

```bash
type 程序安装目录\auto_order_daemon.log
```

日志示例：
```
[2026-06-08 09:38:50] 🍽️ 磐安中学智慧食堂 - 自动订餐
[2026-06-08 09:38:50] ✅ 登录成功 - ***
[2026-06-08 09:38:50] 💰 当前余额: ¥194.3
[2026-06-08 09:38:50] 🔍 开始检查可订餐次...
[2026-06-08 09:38:52] ✅ 2026-06-11 早餐 已订 套餐E ¥5.0
[2026-06-08 09:38:53] 🎉 本次完成！成功订了 1 餐
```

## 配置修改

配置与 GUI 共用 `auto_order_config.json`，可直接编辑：
```json
{
  "keywords": ["排骨", "牛肉", "猪蹄"],
  "exclude_keywords": ["辣椒"],
  "interval_minutes": 30,
  "max_meals_per_day": 6,
  "min_balance": 5.0
}
```

也可在 GUI 的 `🤖 自动订餐` 页面修改后保存，守护脚本会自动读取最新配置。
