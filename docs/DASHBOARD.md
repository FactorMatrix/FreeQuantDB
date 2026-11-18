# 数据工作台

主导航只有六项：概览、数据集、下载、质量、提供、设置。

- 打开页面时读取本地 `catalog/`。概览和数据集按其中的市场、资产、类型、频率及包路径分组。
- 「更新磁力链接」从配置的目录源同步说明文件。同步失败时保留本地目录，页面显示错误。
- 包详情分别展示磁力状态、本地数据状态、目录预期和 Parquet 实测值。日期、字段、行数和标的数按证据显示。覆盖可手动核对。
- 下载先展示任务，可按包直接下载，也可预览日、月、年或自定义范围。没有磁力的本地包照常展示。
- 查询支持标的和日期筛选、字段选择与分页。质量页列出具体异常、包路径、预期值和实测值。
- 提供页点击“开始做种”即可检查并提供当前目录内可用的本地数据，点击“停止做种”结束。范围、上传上限与节点属于可选高级设置；已有设置继续生效。没有数据、文件缺失和等待连接会分别提示，不把零连接说成正在传输。本地发布、独立汇总与来源管理在同组页签。
- 设置管理传输、节点、订阅、目录源、存储路径与日志。

字段、目录、状态和所有按钮均连接当前真实后台，无数据时保持空状态。格式和使用说明见 [行情说明](MARKET_DATA.md)。

普通导入和打开网页不会自动做种。关闭网页只释放管理会话，用户主动开启的下载或持续做种可以继续；停止后空闲服务自动退出。Python 可直接调用 `fqdb.provider.run()` 前台提供，也可用 `start()` 显式持续提供、`stop()` 停止，无需先打开网页。

## 命令行做种

```bash
fqdb -d /path/to/fqdb-data provider start --dataset cn/stock/base/1d
fqdb -d /path/to/fqdb-data provider status
fqdb -d /path/to/fqdb-data provider stop
```

`provider start` 在后台持续做种，默认总上传上限为 1 MB/s（1,000,000 B/s）。已保存的限速继续使用，也可以在启动时用 `--upload-limit` 指定。

通过公网 DHT 提供数据时，先在做种端启用 `fqdb settings --discovery on`，再用 `provider start --listen 0.0.0.0:46881` 启动。公网入口需要可达的 BT 端口和持有完整文件的常开节点。局域网可通过 `fqdb peers add NODE_IP NODE_PORT` 登记已知节点。

```bash
fqdb -d /path/to/fqdb-data provider priority set 192.0.2.10 --level high
fqdb -d /path/to/fqdb-data provider priority list
fqdb -d /path/to/fqdb-data provider priority remove 192.0.2.10
```

将示例地址换成接收方的 IPv4 或 IPv6。目标节点重连后，在多个节点争用带宽时优先获得上传配额，所有节点仍共用总上传上限。
