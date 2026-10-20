## 前提
开通紫鸟账号webdriver权限，请参考 [如何开通Webdriver权限](https://open.ziniao.com/docSupport?docId=99)

更多接口文档参考 [紫鸟webdriver对接文档](https://open.ziniao.com/docSupport?docId=98)

## 安装
使用pip命令安装所需依赖
```bash
pip install -r requirements.txt
```

## 修改配置

config.yaml文件

1、修改webdriver_path（chromedriver驱动存放路径）、client_path（客户端程序路径）和version（浏览器版本 v5或v6）
```yaml
ziniao:
  browser:
    # 浏览器版本 v5或v6
    version: v6
    # 浏览器客户端路径，windows V5程序名为starter.exe，V6程序名为ziniao.exe。mac为客户端程序名称ziniao。linux为客户端程序路径/opt/ziniao/ziniaobrowser
    client_path: D:\ziniao\ziniao.exe
    # 浏览器驱动路径，存放chromedriver的文件夹路径。linux使用内核自带的driver程序，不再额外下载，可不设置
    webdriver_path: D:\webdriver
```

2、修改用户登录信息
```yaml
ziniao:
  # 企业登录的用户信息
  user_info:
    company: 您登录紫鸟浏览器的时候输入的企业公司名
    username: 您登录紫鸟浏览器的时候输入的企业用户名
    password: 您登录紫鸟浏览器的时候输入的密码
```

3、修改运行脚本配置
```yaml
task:
  thread_num: 3
  # 任务列表
  amazon:
#    - feedback_export # 反馈导出
    - customer_voice_export # 客户评价导出
    - business_report_export # 销售和流量业务报表导出
    - orders_export # 订单导出
```

4、按需修改指定店铺运行脚本
```python
# 过滤指定店铺运行脚本(例如美国亚马逊：siteId=1)
amazon_store = [browser for browser in browser_list if browser.get('siteId') == '1']
```

## 运行
运行ziniao_webdriver_demo.py文件
```python
python ziniao_webdriver_demo.py
```

## 每日多平台店铺检查

本仓库已新增飞书控制台驱动的 TikTok、Shopee、美客多并发店铺采集流程。默认并发数为 3，单店关闭后的 DeepSeek 分析在独立线程池运行。完整目录、配置字段、函数职责和部署方式见 [PROGRAM_DESIGN.md](PROGRAM_DESIGN.md)。

三个平台当前统一只采集、推送和写入巴西今天的数据，`config/config.yaml` 的 `periods.today` 可以关闭今天；旧配置即使还包含昨天、7天或30天开关，也不会执行历史周期。二维表和多维表均保存今天经营及广告指标（包括 Shopee 商业分析概述），字段定义集中在 `daily_store_check/storage_schema.py`。首次运行自动新增缺少的今天字段，并在现有电子表末尾追加今天列；历史列和历史记录保留。

多维表已有的公式、关联引用等只读业务字段保留不动，自动新增“原指标名（采集值）”数值字段保存采集结果；身份元数据若为只读则明确报错，不会静默漏写。

多维表查重兼容明确成功且无后续分页的空响应（`items` 省略或为 `null`），结构损坏或总数、分页标记矛盾时仍禁止新增记录。查重会读完全部分页，避免遗漏已有批次。首次补字段只先读取一次完整结构，正常创建不再逐字段重复查询；创建异常后仍回读确认存在性及类型。业务指标仍按店铺一次提交整行，而非逐字段写入。

飞书写入在浏览器确认关闭后执行。每个采集快照携带巴西业务日期、稳定采集批次和指标币种；同一批次补写先回读，已有记录更新或跳过，避免重复追加。结果不确定的电子表请求不会盲目再次追加，状态保存到 `data/write_outbox`。本轮结束后，采集失败才重新打开店铺，写入失败直接补写已保存快照；程序重启后也优先恢复今天未完成的补写。昨天的待补写不会自动上传。

多维表新增记录的 `client_token` 使用飞书要求的 UUIDv4，按应用、表和原采集身份持久化到 `data/bitable_client_tokens`；同一批次重试、重启复用同一令牌。该目录应与采集快照一起保留，不要在补写期间删除。

显式补写历史快照使用 `python replay_write_outbox.py --dates 2026-10-14 2026-10-15` 预览，加 `--apply` 才实际执行。省略日期则检查全部待补写批次。补写严格保留原业务日期、采集时间和批次，只写未完成的目的地，不打开店铺、不重发机器人消息或调用 DeepSeek；执行前备份原快照到 `data/replay_backups`，结果保存到 `data/replay_reports`。日常整轮运行与历史补写共享进程锁，避免同时追加。

电子表追加前先保存本机在途状态。追加超时或进程中断后，补写及重启只核验原批次，未确认落地就保留失败状态，不再次追加；需人工核对确认未写入后才能恢复追加。明确服务端拒绝的请求仍可以正常有限重试。

TK 广告订单和概览订单分别为“今天广告SKU订单数”“今天概览SKU订单数”。消息、飞书和 DeepSeek 共用今日字段；每项金额保留原始 USD/BRL 币种，模型禁止跨币种直接计算或把经营订单当自然订单。日期按钮验证默认等待5秒，可用 `platforms.tiktok.date_step_wait_seconds` 调整；可恢复等待记为运行明细，最终失败才记录错误。

美客多广告概览优先读取浏览器触发的 `chart` 响应。当天指标不完整或监听超时时，点击“周期 → 自定义”，以巴西 UTC-3 的完整日期选择今天，并在日期双击前重新准备监听；只接受起止均为今天的 `chart`。单日响应中的空日期数组可以使用同一响应的 `summary` 补齐，数值 `0` 属于有效数据。若单日响应仍不可用，确认页面为今天且指标加载稳定后，仅读取投资额、收入、销量（中文页面显示“销售额”）、新增粉丝和访问量；不生成缺失的曝光数或点击量。旧广告活动管理页保留独立的 `campaigns/search` 流程。整个广告采集只使用浏览器监听及页面操作，不发送主动接口请求。等待与双击间隔参数见 `config/config.example.yaml` 的 `platforms.mercado.ad_capture`。

广告页按需关闭已知提示弹窗：TikTok 先执行原日期点击流程，仅在广告页日期元素未找到、点击异常或点击后状态验证失败时检查导航更新弹窗；有弹窗则点击“知道了”，确认关闭后重试日期，没有则继续原重试。进入广告页、点击前及指标等待时不主动探测，也不预先等待弹窗出现。美客多按预算推荐弹窗的结构定位“错过这个机会”，在广告监听和日期操作期间拒绝推荐，保持原监听不中断。两平台均确认整个弹窗连续两次不可见后继续，单次关闭等待默认5秒、最多尝试3次；没有弹窗时正常执行。

```powershell
pip install -r requirements-daily.txt
Copy-Item config/config.example.yaml config/config.yaml
# 正式运行：进程常驻，每天按 config.yaml 的时间执行。
python run_daily_store_check.py

# 立即执行第一轮，完成后仍常驻并等待第二天。
python run_daily_store_check.py --run-now

# 仅用于临时测试：立即执行一轮后退出。
python run_daily_store_check.py --run-now --once
```

每家店铺采集成功后，原始结果先写入 `data/captures`；确认紫鸟店铺浏览器关闭后，尝试写入飞书并发送原始消息。即使写表失败，原始消息及已启用的单店分析仍继续；结果分别记录“采集状态”“写入状态”“写入错误”。消息成功及单店分析提交进度单独持久化，同批次补写不重复推送或分析。AI输入保留采集成功，并另外列出写入故障。

首轮采集、登录或关店失败时，暂不写入飞书、不发送失败消息。首轮所有已启动店铺结束后，失败店铺按控制表顺序串行补跑一次：成功则正常写表、推送并按原开关进行 DeepSeek 分析；仍失败则发送最终失败消息，不写入失败数据。经理汇总只保留每个任务的最终结果，单店计时包含首轮和补跑耗时。若有浏览器未确认关闭，沿用停止开店的保护，跳过补跑并统一通知最终失败。

飞书多维表和电子表不支持跨接口事务：若写表本身发生部分失败，已经明确成功的目标不能撤回，补跑时不会重复追加该目标，只重试失败的目标。本机 JSON 仍可用于人工核对和补写。

`data/daily_store_check.log` 只保留警告和错误；点击、XPath 和原始值写在 `data/daily_store_check.debug.log`。

## 扩展脚本
### 一、增加亚马逊店铺任务
1、创建脚本文件，例如： feedback_export.py

2、创建脚本任务函数，函数参数为driver、store_name、download_path，
例如：def amazon_feedback_export(driver, store_name: str, download_path: str):

3、修改ziniao_webdriver_demo.py文件，func_dict中添加任务函数
```python
func_dict = {
    "feedback_export": amazon_feedback_export
}
```
4、修改config.yaml配置文件，task_list中添加任务，例如：
```yaml
task:
  # 任务列表
  amazon:
    - feedback_export # 反馈导出
```

### 二、自定义自动化操作
修改use_one_browser_run_task函数中自动化操作部分
```python
# 打开店铺平台主页后进行后续自动化操作
print(f"=====进行脚本执行：{store_name}=====")
    # todo 自定义自动化操作
print(f"=====脚本全部执行完毕：{store_name}=====")
```
