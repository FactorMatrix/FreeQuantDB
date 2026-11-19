# 数据下载目录

目录包含 `cn/stock/base` 的 2005 年股票日线年包，覆盖 2005-01-04 至 2005-12-30，包含 329,290 行、1,369 个标的、242 个交易日。当前年包已修订连续停牌期间的前收盘价，更新磁力目录后可下载新版。[根索引](index.json) · [日线规格](cn/stock/base/1d/README.md) · [2005.parquet 包说明](cn/stock/base/1d/2005/2005.parquet.json)

安装支持磁力目录 v5 的 FQDB 后，可在看板点「更新磁力链接」，也可用命令行同步。官方入口固定，新包和修订共用这个地址。

```bash
fqdb catalog refresh
fqdb plan cn/stock/base --frequency 1d --year 2005
fqdb download cn/stock/base --frequency 1d --year 2005
```

同步后，下载使用本地包说明中的磁力链接。新安装默认启用 DHT 发现。如果当前目录曾关闭发现，可用 `fqdb settings --discovery on` 重新启用。也可以用 `fqdb peers add NODE_IP NODE_PORT` 指定已知节点。

独立镜像沿用根索引和相对目录结构。用 `fqdb catalog add MIRROR_URL` 配置并同步镜像，或用 `fqdb catalog import /path/to/catalog` 导入本地说明。已同步的磁力在目录服务离线时仍可使用。

```bash
fqdb catalog check cn/stock/base/1d/2005/2005.parquet.json
```

[官方 Raw 目录](https://raw.githubusercontent.com/FactorMatrix/FreeQuantDB/main/catalog/index.json)
