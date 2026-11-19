# 数据包格式

FreeQuantDB 管理符合约定的 Parquet 数据包。市场、资产、数据类型、频率和期间由目录确定。

## 目录结构

默认数据根目录是 `~/.fqdb`。运行 `fqdb gateway -d ~/fqdb_data` 可指定根目录并保存该路径。

数据包相对于根目录的路径为 `data/<市场>/<资产>/<数据类型>/<频率>/<数据包相对路径>`。分包周期和文件名以各数据集的目录为准。

以下是 A 股 2005 年日线包的目录示例。

```text
catalog/cn/stock/base/1d/index.json
catalog/cn/stock/base/1d/2005/2005.parquet.json
data/cn/stock/base/1d/2005/2005.parquet
```

数据集索引记录字段、标的列和时间列。包说明记录实际日期范围、行数、标的数、交易日数、字节数和磁力链接。手工维护的本地包可以省略磁力链接。

## 内置行情字段

| 频率 | 字段 |
| --- | --- |
| 日线 | `instrument`、`trading_date`、`open`、`high`、`low`、`close`、`volume`、`amount`、`prev_close` |
| 分钟 | `instrument`、`trading_time`、`open`、`high`、`low`、`close`、`volume`、`amount` |

`instrument` 是字符串，`trading_date` 是日期，`trading_time` 含时区，量价字段是双精度数值。SDK 按本地数据离线查询。缺少可信交易日证据时，跨夜记录的所属交易日显示为待核对。

## 下载与核对

官方磁力说明可同步到本机，也可从同结构的镜像或本地文件导入。下载完成采用 BitTorrent 分片校验结果。包说明和实际文件可在看板中核对，或用 `fqdb catalog check` 比较预期与实测值。[数据下载](../catalog/README.md)
