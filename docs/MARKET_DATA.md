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
| 日线通用 | `instrument`、`trading_date`、`open`、`high`、`low`、`close`、`volume`、`amount`、`prev_close` |
| 期货日线新增 | `settlement`、`prev_settlement`、`open_interest`、`limit_up`、`limit_down` |
| 分钟 | `instrument`、`trading_time`、`open`、`high`、`low`、`close`、`volume`、`amount` |

`instrument` 是字符串，`trading_date` 是日期，`trading_time` 含时区，量价字段是双精度数值。SDK 按本地数据离线查询。缺少可信交易日证据时，跨夜记录的所属交易日显示为待核对。

期货 `instrument` 包含合约代码和交易所后缀。大商所 `L`、`PP` 和 `V` 的月均价合约保留代码中的 `F`，例如 `L2602F.DCE`。[期货日线字段口径](../catalog/cn/future/base/1d/README.md)

指数日线的量价字段保留各指数的口径，`close` 是必填的正数点位。开、高、低、前收盘参考点位和量额缺失时以 Parquet 空值表示。指数代码可使用 `.INDX` 命名空间。[指数日线覆盖与字段](../catalog/cn/index/base/1d/README.md)

## 下载与核对

官方磁力说明可同步到本机，也可从同结构的镜像或本地文件导入。下载完成采用 BitTorrent 分片校验结果。包说明和实际文件可在看板中核对，或用 `fqdb catalog check` 比较预期与实测值。[数据下载](../catalog/README.md)
