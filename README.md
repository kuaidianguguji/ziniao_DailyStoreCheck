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

三个平台的抓取周期可以在 `config/config.yaml` 中分别开关：TikTok、Shopee 使用 `today`、`yesterday`、`7d`；美客多使用 `today`、`7d`、`30d`。将某个值设为 `false` 后，该周期不会执行页面切换和指标读取，其余周期照常运行。

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
