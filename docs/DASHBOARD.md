# 数据工作台

主导航分为「数据」和「管理」。在「数据」下查看概览、数据集和质量，在「管理」下打开传输和设置。

- 打开页面时读取本地 `catalog/`。概览显示各市场和资产的本地数据包覆盖。点击覆盖条可筛选下方数据集。数据集页支持搜索、排序和按市场、资产、频率、状态筛选。
- 「更新磁力链接」从配置的目录源同步说明文件。同步失败时保留本地目录，页面显示错误。
- 包详情分别展示磁力状态、本地数据状态、目录预期和 Parquet 实测值。日期、字段、行数和标的数按证据显示。覆盖可手动核对。
- 传输页先展示任务，可按包直接下载，也可预览日、月、年或自定义范围。展开「传输设置」可设置订阅、查看检查结果、暂停或恢复共享、设置限速和管理优先 IP。
- 查询支持标的和日期筛选、字段选择与分页。质量页列出具体异常、包路径、预期值和实测值。
- 数据集页组也提供本地发布与数据导入。设置页管理任务并发、节点、目录源、存储路径与日志。「订阅」可直接进入传输设置。

字段、目录、状态和所有按钮均连接当前真实后台，无数据时保持空状态。格式和使用说明见 [行情说明](MARKET_DATA.md)。

`fqdb gateway`、正常业务命令和普通 `import fqdb` 都会启动或连接当前数据目录的 BT 后台。只有 Gateway 打开网页。下载时通过 BT 分享已下载的分块，完成后沿用同一个 info-hash 做种。关闭网页、终端或脚本后，后台继续处理下载、共享和启用的订阅。运行 `fqdb service stop` 停止整个后台。重启电脑后，在使用 CLI 或重新导入包时启动。

帮助、状态和停止命令可以在后台关闭时使用。网页或 `fqdb.provider.stop()` 保存的共享暂停设置会保留。需要显式恢复时，可在网页开启共享或调用 `fqdb.provider.start()`。

## 自动更新与订阅

后台启动时会先检查已保存的订阅，后续默认每 3 小时检查一次。可以调整间隔，或指定每天的时间和时区。留空数据集时，订阅仅更新磁力目录。

勾选自动下载后，选择要同步的数据集和频率。填写日期可补齐指定范围，留空日期则跟进新发布和新版本。首次拿到该范围的目录时只取最新覆盖。已下载或已覆盖的数据会跳过，手动暂停、取消的任务仍由用户控制。

传输设置显示上次检查结果和下次检查时间。目录源暂时不可用时，可以查看错误，本地已有的数据仍可继续共享。

也可用 CLI 或 Python 保存订阅。

```bash
fqdb subscribe cn/stock/base --frequency 1d --download
```

```python
import fqdb

fqdb.subscribe("cn/stock/base", frequency="1d", download=True)
```

## 命令行做种

```bash
fqdb -d /path/to/fqdb-data provider start --dataset cn/stock/base/1d
fqdb -d /path/to/fqdb-data provider status
fqdb -d /path/to/fqdb-data provider stop
```

传输页分别显示当前总上传上限和保存的纯做种上限。

| 传输状态 | 总上传上限 |
| --- | --- |
| 正在接收下载分片 | 不限速，包括同一后台正在上传的旧种子 |
| 没有下载分片传输 | 恢复保存的纯做种上限，新安装默认 500 KB/s（500,000 B/s） |

网页使用 MB/s，500 KB/s 显示为 0.5 MB/s。`provider start --upload-limit` 使用 B/s。将纯做种上限设为 `0` 时，纯做种也不限速。

新安装默认启用 DHT、PEX 等节点发现，并在本机 IPv4 网络接口上监听 BT。已有监听地址和发现设置继续生效，管理网页默认只监听 `127.0.0.1`。公网可达性取决于 NAT、端口和对端在线情况。局域网也可通过 `fqdb peers add NODE_IP NODE_PORT` 添加已知节点。

```bash
fqdb -d /path/to/fqdb-data provider priority set 192.0.2.10 --level high
fqdb -d /path/to/fqdb-data provider priority list
fqdb -d /path/to/fqdb-data provider priority remove 192.0.2.10
```

将示例地址换成接收方的 IPv4 或 IPv6。IP 规则变更在目标节点重连时生效。纯做种限速下，多个节点争用带宽时，libtorrent 会优先给指定 IP 分配可用带宽。下载期间解除总上限后，优先 IP 的效果可能不明显，实际速度取决于可用带宽。
