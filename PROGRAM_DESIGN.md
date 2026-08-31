# 紫鸟每日店铺检查程序设计

## 1. 目标与边界

程序每天在配置时间执行一次，从飞书多维表“控制台”读取店铺名、推送人员、开关和平台。开关为空或暂停时不处理；其余店铺按照 `data.store_concurrency` 并发执行，默认同时打开并采集 3 间紫鸟店铺。每间店铺独立完成采集、飞书写入、原始消息推送和关闭确认。

短期多维表 `TK_数据`、`SP_数据`、`MKD_数据` 只保留最近 `retention_days` 天，默认 90 天。三个飞书电子表只追加，不删除，作为完整历史记录。

## 2. 目录职责

```text
ziniao_DailyStoreCheck_codex_two/
├─ config/config.example.yaml          # 可提交的配置模板，不含真实凭据
├─ config/config.yaml                  # 本机运行配置，已被 Git 忽略
├─ daily_store_check/
│  ├─ config.py                        # 配置加载、平台名标准化、开关判断、StoreTask
│  ├─ deepseek_client.py               # 单店及 ALL_info 的 DeepSeek 请求和响应解析
│  ├─ feishu_client.py                 # 飞书 token、多维表、电子表、机器人
│  ├─ ziniao_client.py                 # 紫鸟 IPC、店铺开关、WebDriver 会话
│  └─ orchestrator.py                  # 并发业务流程和平台爬虫注册
├─ tiktok/TK_auto.py                   # TikTok 独立自动化和爬虫
├─ shopee/SP_auto.py                   # Shopee 独立自动化和爬虫
├─ mercado/MKD_auto.py                 # 美客多独立自动化和爬虫
├─ run_daily_store_check.py            # 生产入口和每日时间调度
├─ requirements-daily.txt              # 本项目新增依赖
└─ ziniao_webdriver_demo.py             # 官方示例，保留作对照
```

## 3. 核心执行流程

1. `run_daily_store_check.main` 读取 YAML，默认常驻后台并调用 `wait_for_schedule` 等待每日计划时间；单轮任务异常会记录后继续等待下一次执行。
2. `DailyStoreCheck.run_once` 调用 `FeishuClient.list_control_tasks`，过滤空开关、暂停和未知平台。
3. `_prepare_ziniao` 启动紫鸟客户端、更新内核、读取完整店铺列表。
4. `_run_store` 先用控制表店铺名精确匹配紫鸟 `browserName`；精确名称不一致时，按控制表平台和名称中的店号生成“平台+店号”键，再匹配紫鸟名称中的同一平台和店号，得到 `browserOauth` 或 `browserId`。
5. `ZiniaoStoreSession` 严格调用官方 `startBrowser` 打开一间店铺，连接 WebDriver，完成 `ipDetectionPage` 检测后打开 `launcherPage`；当前紫鸟 V6 环境中先 `driver.quit` 会使 16851 IPC 失联，因此 `with` 结束时先调用官方 `stopBrowser` 确认关闭店铺，再清理 WebDriver 会话。
6. `_load_crawler` 根据配置动态载入 `TK_auto.py`、`SP_auto.py` 或 `MKD_auto.py`。
7. 爬虫优先用 DrissionPage 连接紫鸟的 `debuggingPort`，输出统一结构。
8. `_write_feishu` 写入对应数据多维表，并追加对应历史电子表。
9. `_safe_notify` 根据“推送人员”的 `open_id` 使用应用机器人定向推送；没有人员 ID 时可退回 webhook。
10. TikTok、Shopee、美客多店铺的原始消息发送且紫鸟店铺关闭成功后，主流程立即把该店完整数据提交到独立 DeepSeek 线程池，并释放采集线程启动下一个店铺。三个平台共用密钥、模型和并发数，分别使用独立系统提示词；当前 TikTok 和美客多暂时回退到虾皮提示词。单店结果以 Markdown 回发对应运营人员；全部单店分析完成后仍会分析非空 `ALL_info`，再按 `robot.summary_recipients` 中的姓名和 `open_id` 逐人发送汇总结果。
11. `_cleanup_retention` 清理三张短期多维表中的过期数据，并退出紫鸟客户端。

## 4. 文件、函数和关键变量

### `config/config.example.yaml` 与本机 `config/config.yaml`

首次部署时复制示例文件为 `config/config.yaml`，再填写本机路径和凭据。真实配置已被 Git 忽略。

- `schedule.run_time`：每日执行时间，格式 `HH:MM`。
- `schedule.timezone`：时区，默认 `Asia/Shanghai`。
- `schedule.run_once`：`false` 表示启动后常驻并每天循环（默认）；`true` 表示执行一轮后退出，适合由 Windows 任务计划程序负责每天启动。
- `feishu.app_id/app_secret`：飞书自建应用凭据，也可用环境变量覆盖。
- `feishu.bitable.control`：控制台多维表的 `app_token + table_id`。
- `feishu.bitable.data_tables`：三个短期多维表分别填写自己的 `app_token + table_id`。
- `feishu.bitable.default_app_token`：如果四张多维表属于同一个应用，可以填公共 token，子项只填 `table_id`。
- `spreadsheets`：三个历史电子表分别填写 `token + sheet_id`；也可以额外指定完整 `range`。
- `deepseek.api_key/system_prompt`：DeepSeek API Key 和固定系统提示词；API Key 也可用环境变量 `DEEPSEEK_API_KEY` 覆盖。
- `data.store_concurrency`：店铺采集与单店 DeepSeek 分析的并发数，默认 `3`；两个任务池相互独立，AI 等待不会占用浏览器采集名额。
- `platforms.tiktok.periods`、`platforms.shopee.periods`：分别控制今天、昨天、7天是否抓取；每项为布尔值，缺省时默认开启。
- `platforms.mercado.periods`：控制今天、7天、30天是否抓取；每项为布尔值，缺省时默认开启。
- `deepseek.single_store_enabled`：是否在每个平台单店原始消息后调用 DeepSeek 并回发分析；设为 `false` 时只跳过单店分析，不影响原始数据推送和整轮汇总。
- `retention_days`：短期多维表保留天数。
- `platforms.*.crawler`：平台到 Python 爬虫类的映射，格式 `模块:类名`。

### `daily_store_check/config.py`

- `load_config`：读取 YAML，并允许 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`ZINIAO_*` 环境变量覆盖敏感字段。
- `normalise_platform`：兼容 `TK`、`SP`、`MKD`、`meicado`、`mercado` 等写法。
- `is_enabled_switch`：只有明确的开启值才执行；暂停、空值和其他未知值都不执行。
- `StoreTask`：控制表记录的内部数据类，关键变量为 `store_name`、`recipient`、`platform`。

### `daily_store_check/feishu_client.py`

- `_get_tenant_token`：获取并缓存租户 token。
- `list_records`：处理多维表分页。
- `list_control_tasks`：读取并过滤控制表，人员字段优先解析成 `open_id`。
- `batch_create_records`：每 500 条分批写入数据多维表。
- `remove_old_records`：按采集时间计算 90 天截止线并批量删除过期记录。
- `append_spreadsheet_rows`：向历史电子表追加二维数组。
- `send_robot_message`：优先应用机器人按 `receive_id_type` 定向发送；webhook 只作为固定群机器人兜底。`send_robot_markdown_message` 为 DeepSeek 汇总发送 Card 2.0 `interactive` 消息，卡片正文使用 `tag=markdown`。
- `configured`：凭据是否齐全；不齐全时读写接口进入 dry-run 并记录日志。

### `daily_store_check/ziniao_client.py`

- `send_http`：官方本地 HTTP IPC 通道。
- `start_client`：按 Windows/macOS/Linux 拼接紫鸟启动参数。
- `update_core`：轮询内核更新状态，最多等待约 120 秒。
- `list_browsers`：读取紫鸟店铺列表。
- `open_store/close_store`：完整复用官方 `startBrowser/stopBrowser` 请求字段，一对一控制店铺生命周期。
- `open_ip_check/open_launcher_page`：复用官方示例的 IP 检测页和平台主页启动步骤。
- `get_driver`：优先使用紫鸟内核自带 webdriver，找不到时按内核主版本查找下载目录。
- `ZiniaoStoreSession`：关键安全边界；任何成功、异常或爬虫失败都执行关闭动作。紫鸟 IPC 失联时会重启客户端并重试 `stopBrowser`；连续三次仍无法关闭时中止后续店铺，确保不会同时打开下一家。
- `browser_oauth`：当前已打开店铺的真实紫鸟标识。
- `opened`：紫鸟返回的调试端口、下载目录、启动页等会话数据。

### 三个平台独立文件

- `TK_auto.py`、`SP_auto.py`、`MKD_auto.py` 各自维护 DrissionPage 连接、标签页获取、页面操作、数据提取和结果组装。
- 三个平台故意不共享爬虫基类；即使逻辑重复，也保留在各自文件中，方便后续独立修改。
- 每个文件使用 `Chromium(紫鸟调试端口)` 接管浏览器，再通过 `browser.latest_tab` 获取当前标签页。
- 三个平台都接管紫鸟已经打开的当前标签页；TikTok 会在该标签页中直接进入固定广告页和数据概览页，美客多会进入经营指标页，均不会创建普通浏览器。
- 三个平台当前选择器是骨架示例。登录真实后台后，应以账号当前 DOM 为准替换选择器和指标。
- `MKD_auto.py` 已列出 7 天和 30 天共 30 个指标，对应美客多多维表除店铺名、采集时间外的 30 个字段；空 XPath、找不到元素或解析失败均写入空值。
- `MKD_auto.py` 先读取紫鸟当前标签页网址：网址包含 `vendedores` 时进入卖家后台经营指标页，否则进入 `https://www.mercadolivre.com.br/metricas#sc-menu`；`PERIOD_CLICK_STEPS` 负责点击日期按钮、最近 7 天和最近 30 天选项，最后一个日期选项点击后固定等待 30 秒再读取数据。
- 美客多“进度”字段的百分比按数值比例写入，例如页面 `12.5%` 写入 `0.125`，由飞书字段格式显示为 `12.5%`；多维表和历史电子表严格使用 32 字段顺序。
- 美客多运行日志记录页面状态、菜单分支、按钮 XPath、每次点击和重试、日期等待，以及每项指标的完整 XPath、读取重试、原始值/类型、转换值/类型、币种和最终打包 JSON，便于按字段排错。
- 美客多日期按钮只有在对应的“最近 7 天/最近 30 天”选项出现后才判定点击成功；日期选项只有在点击后连续两次检测为不可见才判定成功。状态验证失败会重新点击原按钮，而不是继续执行后续采集。
- 美客多货币指标按数值保留两位小数，指标定义标记 `currency_code=BRL`；数量为整数，转换率为不带小数的百分数字符串。
- 美客多返回的 `飞书字段` 会被编排器合并到多维表中的同名字段，历史电子表也会追加这些指标值。
- `TK_auto.py` 确认登录后直接进入 TikTok 固定广告页和数据概览页，各自集中维护 URL、时间面板/日期切换步骤和指标 XPath；未填写或暂时失效的 XPath 按空值处理。
- TikTok 广告金额字段使用 `currency_code=USD`，概览 GMV 使用 `currency_code=BRL`；两个模块分别返回一条记录，避免同名 SKU 订单数字段相互覆盖。
- TikTok 接管后立即同时检查登录入口、营销按钮、店铺广告按钮、首页广告弹窗关闭按钮和 `verify-bar-close` 验证码弹窗关闭按钮，不等待 `document.readyState`。首次只看到登录入口时固定等待 `LOGIN_RECHECK_WAIT_SECONDS` 后复查，复查仍只有登录入口才执行登录；登录流程每一步前后都会检查四类已登录标志，发现任一标志便立即结束登录流程。确认登录后直接打开广告页和数据概览页，时间按钮及日期选项的出现/消失统一在 `TK_STEP_WAIT_SECONDS` 内确认，随后等待页面完成和数据标志再批量抓取。
- TikTok 登录默认直接提交；仅在出现“请检查输入的手机号格式”时切换邮箱并重新登录。登录提交后如果出现物体匹配验证码，程序从内存读取图片，调用验证码识别接口识别两个相同物体。接口返回 `data.res_str` 字符串，例如 `"[(135, 87), (287, 179)]"`，表示图片上的两个点击坐标，图片左上角为原点；程序直接使用坐标，不做五次采样、平均或四舍五入，再通过当前紫鸟标签页的 CDP 鼠标事件点击并提交。API Key 配置在 `platforms.tiktok.captcha.api_key`，也可使用环境变量 `TIKTOK_CAPTCHA_API_KEY`。
- TikTok 同一日期周期的全部指标通过一次页面 JavaScript 批量 XPath 读取；批量脚本异常时才回退为逐项读取。日志会逐项打印字段名、完整 XPath、该 XPath 抓到的原始文本和类型，再打印转换结果。
- TikTok 日期按钮采用“首次 + 3 次重试”，相邻尝试至少间隔 2 秒；时间面板出现和日期选项消失都以统一的 `TK_STEP_WAIT_SECONDS`（默认5秒）作为单次状态确认超时。
- TikTok 会详细记录页面状态、按钮 XPath、每次点击和重试结果、下一元素等待、弹窗处理，以及每个指标的原始值、转换值和 Python 类型；日志同时显示在控制台并写入 `data/daily_store_check.log`。
- TikTok 写入多维表时仍使用原有33字段定义；公式字段不发送、空数值不发送、日期转换为毫秒时间戳。机器人改用 Markdown 卡片，按“昨天广告、7天广告、昨天概览、7天概览”分组并依据 XPath 原始文本补充 `$` 或 `R$`。历史电子表仍为33列，但使用 `TIKTOK_SPREADSHEET_FIELD_ORDER` 按相同业务分组重新排列。
- `MKD_auto.py` 接管紫鸟当前标签页并等待初始页面加载完成，按当前网址选择经营指标页后再次等待页面加载，再额外等待 10 秒处理首页广告。所有按钮采用首次点击加 3 次重试，重试间隔至少 2 秒，点击后最多等待 30 秒让下一按钮或数据出现。
- `SP_auto.py` 接管首页后立即检查广告弹窗，页面加载完成后再检查一次；随后判断“Shopee广告”是否可见，不可见时点击“营销中心”并以“Shopee广告”出现作为展开成功条件。
- Shopee 刚接管紫鸟标签页时会优先检查同时包含 `password` 和 `loginKey` 输入框的登录表单，页面加载后、任何菜单操作前再复查一次；发现该表单便停止当前店铺采集，由紫鸟会话关闭店铺并在关闭确认成功后继续下一店铺。未发现该表单时，仍按 `LOGIN_BUTTON_XPATHS` 的顺序处理其他登录按钮。
- Shopee 广告入口采用“首次点击 + 8 次重试”，每次点击后等待约 3 秒确认时间切换按钮出现；未出现时检查广告弹窗再重试。确认进入广告页后只滚动一次时间按钮到视口水平中心线，滚动失败不重试。打开日期面板及选择昨天/最近 7 天也分别采用“首次点击 + 8 次重试”；面板选项未出现时检查弹窗，面板打开和日期切换成功后各单次定位时间按钮。日期选项连续两次不可见才认定选择成功，最终失败会停止当前店铺，避免读取上一时间范围的旧数据。
- Shopee 采集昨天和最近 7 天各 12 个 ALL 指标。巴西金额如 `R$18.558,26` 转为 `18558.26`，广告支出回报率如 `7,61` 转为 `7.61`；优惠价金额、优惠劵带来销售额使用金额字段（真实飞书表字段使用“劵”字），展示次数、点击数、订单量、商品已出售和加购次数写入数值字段，加购率和点击率按飞书“小数”字段写入去掉百分号后的两位数值；缺失 XPath、元素或无效文本均返回空值并记录原因。
- Shopee 日志记录菜单可见性判断、每次按钮查找/点击/重试、日期面板出现和消失、指标原始值与转换值及其 Python 类型，以及最终打包 JSON。编排器把 24 项指标合并为一条严格匹配 26 字段多维表的记录；空金额或小数字段不会放入多维表请求体，历史电子表固定按 26 列追加。
- Shopee 数量字段支持页面缩写，`10.7k`/`10,7k` 转为 `10700`，`1.2m` 转为 `1200000`；不带缩写的 `1.234` 仍按千位分隔转换为 `1234`。
- Shopee 每个指标同时生成飞书纯数字和消息显示值：`3,79%` 显示为 `3.79%`、`6,34` 显示为 `6.34`、`R$43,56` 显示为 `R$43.56`、`R$17.490,26` 显示为 `R$17490.26`。多维表/电子表使用纯数字，机器人和 `ALL_info` 使用显示值。
- Shopee 写入多维表前读取并缓存真实字段列表，自动兼容“券/劵”和首尾空格差异；真实表中不存在的非关键字段会单独跳过，不能再使整条记录触发 `FieldNameNotFound`。多维表和历史电子表独立写入，一方失败时仍会尝试另一方。

### `daily_store_check/orchestrator.py`

- `run_once`：一轮完整任务；使用独立线程池并发执行店铺采集和单店 DeepSeek 分析，所有单店结果完成后再执行最终汇总。
- `_run_concurrent_stores`：按固定并发数滚动提交店铺任务；店铺关闭后立即提交 AI 分析并补充下一个采集任务，结果仍按控制表顺序写入 `ALL_info`。
- `_find_browser_identifier`：先执行 Unicode/空白归一化后的精确匹配；失败后提取 `tiktok/TK`、`shopee/虾皮`、`mercado/美客多`及店号，按“平台:店号”匹配，支持公司名前缀、空格和末尾“店”等紫鸟名称差异。
- `_load_crawler`：动态加载平台类，新增平台时不需要修改循环逻辑。
- `_write_feishu`：一份标准爬虫结果同时转换为多维表记录和电子表行。
- `_cleanup_retention`：只清理短期多维表。
- `_safe_notify`：消息推送失败不会阻断下一个店铺。
- `_extract_all_info_values`：统一提取三个平台的全部指标，空指标也保留在 `ALL_info` 中方便排错。
- `_send_all_info_summary`：整轮任务末尾调用 DeepSeek 分析非空 `ALL_info`，再把返回 Markdown 通过 interactive 卡片发送给全部 `summary_recipients`。
- `_print_store_processing_times`：最末尾先打印各店铺耗时字典，再打印包含总时间、TK 总时间、虾皮总时间和美客多总时间的汇总字典；汇总统一使用累计“分钟+秒”格式。

### `daily_store_check/deepseek_client.py`

- `DeepSeekClient.analyze_all_info`：将结构化 `ALL_info` 序列化为 JSON 文本，调用 Chat Completions 接口并严格解析 `choices[0].message.content`。
- `DeepSeekClient.analyze_store`：将单个平台店铺的完整数据序列化为 JSON，按 `tiktok_system_prompt`、`shopee_system_prompt`、`mercado_system_prompt` 选择提示词并同步分析；返回内容由编排器发送给该店铺控制表中指定的运营人员。
- API Key、系统提示词、模型、地址、温度、输出长度和超时均来自 `deepseek` 配置节点；日志不会输出 API Key。HTTP 失败、响应解析失败、响应结构错误或返回文本为空时，首次失败后按配置最多重试 5 次。

### `run_daily_store_check.py`

- `configure_logging`：控制台和 5 MB 滚动文件日志，保留 5 个备份。
- `wait_for_schedule`：按配置时区等待每日时间，每 60 秒重新计算一次；一轮完成后即使当前仍早于当天计划时间，也会等待到第二天，避免同一天重复运行。
- `--run-now`：立即执行第一轮，完成后不退出，继续常驻等待第二天的计划时间。
- `--once`：明确要求执行一轮后退出；临时测试通常组合使用 `--run-now --once`。

## 5. 飞书准备清单

### 推荐配置关系

```yaml
feishu:
  app_id: "飞书自建应用 App ID"
  app_secret: "飞书自建应用 App Secret"
  bitable:
    control:
      app_token: "控制台多维表所属应用 token"
      table_id: "控制台数据表 table_id"
    data_tables:
      tiktok:
        app_token: "TK_数据多维表所属应用 token"
        table_id: "TK_数据数据表 table_id"
  spreadsheets:
    tiktok:
      token: "TK 历史电子表 spreadsheet token"
      sheet_id: "TK 历史电子表内工作表 ID"
```

如果四张多维表在同一个多维表应用中，可把公共 token 填入 `bitable.default_app_token`，每个子表只填写自己的 `table_id`。三张电子表仍然分别填写各自的 `token` 和 `sheet_id`。

1. 给自建应用开放多维表读写、电子表读写和机器人发消息权限，并发布应用版本。
2. 把应用机器人加入需要发送消息的可见范围。
3. 控制台“推送人员”必须使用飞书“人员”字段。接口会从人员字段返回值中读取 `open_id`/`user_id`，不会把姓名文本猜成用户 ID。
4. 控制台店铺名固定填写平台+店号，例如 `美客多11`、`TK3`、`虾皮5`；紫鸟名称可以包含公司名前缀、空格或末尾“店”，程序会按平台+店号匹配。
5. 当前 `robot.receive_id_type` 为 `open_id`；最终汇总接收人在 `robot.summary_recipients` 中按 `姓名: ou_xxx` 填写。字典为空时不发送 DeepSeek 分析结果。
6. TikTok、Shopee、美客多短期表分别严格使用项目定义的 33、26、32 个字段；不要再为 Shopee 创建通用的“指标/数值/原始数据”字段。
7. 多维表“采集时间”使用日期字段，程序写入前统一转换为毫秒时间戳。
8. 三张历史电子表第一行应分别按 `TIKTOK_SPREADSHEET_FIELD_ORDER`、`SHOPEE_TABLE_FIELD_ORDER`、`MERCADO_TABLE_FIELD_ORDER` 建立33、26、32列；TK表头需要同步调整为新的分组顺序。

## 6. 启动方式

```powershell
pip install -r requirements-daily.txt
Copy-Item config/config.example.yaml config/config.yaml
python run_daily_store_check.py --run-now --once
python run_daily_store_check.py
```

推荐直接启动 `python run_daily_store_check.py` 并保持进程常驻，配置 `schedule.enabled: true`、`schedule.run_time: "07:00"`、`schedule.run_once: false`。若使用 Windows 任务计划程序每天启动一次，则改为 `schedule.run_once: true` 并让任务计划程序负责启动，避免两套调度同时生效。
