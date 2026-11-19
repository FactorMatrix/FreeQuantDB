# FreeQuantDB

FreeQuantDB 提供量化行情下载、订阅更新和本地查询。数据以 Parquet 保存，可用 Python 离线读取。

## 快速上手

需要 Python 3.11+ 和 Git。下面以下载 2005 年 A 股日线为例。

```bash
python -m pip install git+https://github.com/FactorMatrix/FreeQuantDB.git
fqdb catalog refresh
fqdb download cn/stock/base --frequency 1d --year 2005
```

下载完成后，用 Python 读取 2005 年第一个交易日的日线。

```python
import fqdb

df = fqdb.load("cn/stock/base", frequency="1d", start="2005-01-04", end="2005-01-04")
print(df.head())
```

也可以运行 `fqdb gateway` 打开网页，点击「更新磁力链接」，在「数据集」中选择数据包下载，并在数据集详情的「行情查询」中预览行情。

## 自动更新

订阅默认每 3 小时检查一次磁力目录。仅同步目录时，数据集可留空。

在网页的「传输 → 传输设置」选择数据集和频率，勾选「自动下载新数据包」，点击「保存订阅」。留空日期可跟进新包和修订，指定日期可补齐该范围内已发布的数据。已有的相同数据会跳过。

也可以用命令行订阅 A 股日线。

```bash
fqdb subscribe cn/stock/base --frequency 1d --download
```

## 数据列表

| 数据 | 数据集 | 频率 | 已上线范围 | 状态 | 预计上线时间 |
| --- | --- | --- | --- | --- | --- |
| A 股基础行情 | `cn/stock/base` | 日线 `1d` | 2005-01-04 至 2025-12-31 | 已上线，其他年份待发布 | |
| A 股基础行情 | `cn/stock/base` | 分钟 `1m` |  | 待上线 | |
| ETF 基础行情 | `cn/etf/base` | 日线 `1d` |  | 待上线 | |
| ETF 基础行情 | `cn/etf/base` | 分钟 `1m` |  | 待上线 | |
| 国内期货基础行情 | `cn/future/base` | 日线 `1d` |  | 待上线 | |
| 国内期货基础行情 | `cn/future/base` | 分钟 `1m` |  | 待上线 | |

A 股日线已发布 2005–2025 年的 21 个年包，共 15,368,408 行。最新可下载范围见[数据下载目录](catalog/README.md)。

## 文档与许可

[管理网页](docs/DASHBOARD.md) · [数据格式与目录](docs/MARKET_DATA.md) · [Python 与开发环境](docs/PYTHON_SUPPORT.md) · [架构](docs/ARCHITECTURE.md)

软件采用 [Apache-2.0](LICENSE) 许可。
