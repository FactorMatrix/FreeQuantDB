# 数据工作台

主导航分为「数据」和「管理」。在「数据」下查看概览、数据集和质量，在「管理」下打开传输和设置。

- 打开页面时读取本地 `catalog/`。概览显示各市场和资产的本地数据包覆盖。点击覆盖条可筛选下方数据集。数据集页支持搜索、排序和按市场、资产、频率、状态筛选。
- 「更新磁力链接」从配置的目录源同步说明文件。同步失败时保留本地目录，页面显示错误。
- 包详情分别展示磁力状态、本地数据状态、目录预期和 Parquet 实测值。日期、字段、行数和标的数按证据显示。覆盖可手动核对。
- 传输页先展示任务，可按包直接下载，也可预览日、月、年或自定义范围。展开「传输设置」可查看后台共享状态、暂停或恢复共享、设置下载与上传上限，以及管理优先 IP。
- 查询支持标的和日期筛选、字段选择与分页。质量页列出具体异常、包路径、预期值和实测值。
- 数据集页组也提供本地发布与数据导入。设置页管理任务并发、节点、订阅、目录源、存储路径与日志。

字段、目录、状态和所有按钮均连接当前真实后台，无数据时保持空状态。格式和使用说明见 [行情说明](MARKET_DATA.md)。

运行 `fqdb gateway` 会启动或连接当前数据目录的 BT 后台，并恢复有原始种子信息的本地数据包。下载时通过 BT 分享已下载的分块，完成后沿用同一个 info-hash 做种。关闭网页或按 `Ctrl+C` 退出 Gateway 后，后台继续处理活跃的下载和做种。网页上的暂停选择会保留到下次启动，运行 `fqdb service stop` 可以停止整个后台。重启电脑后，需要手动运行 Gateway。

普通 `import fqdb` 和帮助命令不会启动后台。Python 也可调用 `fqdb.provider.run()` 前台做种，用 `start()` 显式开启后台做种，或用 `stop()` 暂停做种。

## 命令行做种

```bash
fqdb -d /path/to/fqdb-data provider start --dataset cn/stock/base/1d
fqdb -d /path/to/fqdb-data provider status
fqdb -d /path/to/fqdb-data provider stop
```

默认总上传上限为 1 MB/s（1,000,000 B/s），已有设置继续使用。网页使用 MB/s，`0` 表示不限速。`provider start --upload-limit` 使用 B/s。

新安装默认启用 DHT、PEX 等节点发现，并在本机 IPv4 网络接口上监听 BT。已有监听地址和发现设置继续生效，管理网页默认只监听 `127.0.0.1`。公网可达性取决于 NAT、端口和对端在线情况。局域网也可通过 `fqdb peers add NODE_IP NODE_PORT` 添加已知节点。

```bash
fqdb -d /path/to/fqdb-data provider priority set 192.0.2.10 --level high
fqdb -d /path/to/fqdb-data provider priority list
fqdb -d /path/to/fqdb-data provider priority remove 192.0.2.10
```

将示例地址换成接收方的 IPv4 或 IPv6。设置总上传限速后，规则在目标节点重连时生效。多个节点争用带宽时，libtorrent 会优先给指定 IP 分配可用带宽。所有节点共用总上传上限，优先级没有固定速度或比例。
