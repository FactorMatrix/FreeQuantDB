# FQDB 当前架构

股票、ETF、期货共用固定的日线和分钟格式。见 [行情说明](MARKET_DATA.md)。

## 从哪里改

下列路径以 `src/fqdb/` 为起点。

| 功能 | 入口与职责 |
| --- | --- |
| CLI | `cli/application.py` 的 `CommandLine` 注册参数、选择数据根并执行命令。各命令的参数与处理函数放在 `cli/local.py`、`transfers.py`、`sharing.py` 或 `catalogs.py`。 |
| SDK 和目录 | `api.py` 和公开模块提供 Python 接口，`config.py` 统一解析显式参数、进程选择、环境变量、保存设置和默认目录。 |
| 服务动作 | `service/actions.py` 同时声明处理函数、并发锁与 worker 启动条件。CLI、SDK 与网页经同一套动作调用业务代码。 |
| 任务与 BT | `service/core.py` 的 `Owner` 管理每个数据根的生命周期与锁。`Transfers`、`Seeding` 和 `Checks` 分别负责下载、做种和校验。`service/jobs.py` 管理有限作业，`bt/` 管理原生会话和种子。 |
| 磁力目录 | `catalog/magnet.py` 扫描磁盘和同步目录。`magnet_manifest.py` 校验路径与说明，`verification.py` 核对本地 Parquet。`service/catalogs.py` 生成计划，并在用户下载时登记 BT 任务。 |
| 节点和订阅 | `service/peers.py` 管理节点连接和优先级，`subscriptions.py` 管理活跃会话内的目录刷新。 |
| 行情契约 | `schema/assets.py` 集中声明资产代码、交易所、单位和时区。`schema/market.py` 定义物理列，`schema/digest.py` 负责契约指纹和逐日内容摘要。 |
| 发布 | `publish/publisher.py` 的 `Publisher` 管理符合规范的包和内容修订。`coverage.py` 核对声明的交易日和标的覆盖。 |
| 本地读写 | `storage/metadata.py` 管理 SQLite 数据包、任务和活动版本，`storage/market.py` 选择一致的交易日内容并查询，`validation/` 校验文件、分片和数据质量。 |
| 看板 | `web/dashboard/catalog.py` 将磁盘目录投影为概览、数据集和包详情，`operations.py` 提供任务和运行视图。刷新和下载控件在 `web/static/components/`。 |

新增命令时，在所属 CLI 模块中一起注册参数和处理函数。新增服务动作时，在 `service/actions.py` 明确是否需要启动 worker，以及是否需要持有运行锁。新增看板页时，在 `web/static/routes.js` 注册页面，共享读接口通过 `Dashboard` 注册。资产扩展从 `AssetContract` 开始，并同步验证目录、查询和质量规则。

## 运行边界

离线查询直接读取 SQLite 和 Parquet。有限管理操作复用已运行的 worker，或在持有数据根锁的本地上下文完成。打开 gateway、主动下载或提交作业、主动做种才启动 worker，空闲后退出。gateway 的页面关闭或 Ctrl+C 只释放管理会话。前台做种随调用进程退出，订阅只在 gateway 或做种会话内调度。

## 当前数据根

`catalog/` 存放数据集索引和包说明，`data/` 存放对应的 Parquet。`.state/` 存放同步记录、核对缓存、任务、BT 元数据及内容修订历史。

修订保留原字节与种子，已有下载和做种可继续使用原 info-hash。
