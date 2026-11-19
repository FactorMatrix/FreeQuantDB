# 数据下载目录

公开目录包含 A 股、ETF、LOF 和国内期货的日线年包。更新磁力目录后可按数据集和年份下载。[根索引](index.json)

| 日线数据集 | 覆盖范围 | 年包数 | 记录数 | 最近年包 |
| --- | --- | ---: | ---: | --- |
| [A 股](cn/stock/base/1d/README.md) | 2005-01-04 至 2025-12-31 | 21 | 15,368,408 | [2025](cn/stock/base/1d/2025/2025.parquet.json) |
| [ETF](cn/etf/base/1d/README.md) | 2005-02-23 至 2025-12-31 | 21 | 1,354,052 | [2025](cn/etf/base/1d/2025/2025.parquet.json) |
| [LOF](cn/lof/base/1d/README.md) | 2005-01-04 至 2025-12-31 | 21 | 945,361 | [2025](cn/lof/base/1d/2025/2025.parquet.json) |
| [国内期货](cn/future/base/1d/README.md) | 2005-01-04 至 2025-12-31 | 21 | 2,137,788 | [2025](cn/future/base/1d/2025/2025.parquet.json) |

安装支持磁力目录 v5 的 FQDB 后，可在看板点「更新磁力链接」，也可用命令行同步。官方入口固定，新包和修订共用这个地址。

```bash
fqdb catalog refresh
fqdb download cn/etf/base --frequency 1d --year 2025
fqdb download cn/lof/base --frequency 1d --year 2025
fqdb download cn/future/base --frequency 1d --year 2025
```

同步后，下载使用本地包说明中的磁力链接。新安装默认启用 DHT 发现。如果当前目录曾关闭发现，可用 `fqdb settings --discovery on` 重新启用。也可以用 `fqdb peers add NODE_IP NODE_PORT` 指定已知节点。

独立镜像沿用根索引和相对目录结构。用 `fqdb catalog add MIRROR_URL` 配置并同步镜像，或用 `fqdb catalog import /path/to/catalog` 导入本地说明。已同步的磁力在目录服务离线时仍可使用。

```bash
fqdb catalog check cn/stock/base/1d/2025/2025.parquet.json
```

[官方 Raw 目录](https://raw.githubusercontent.com/FactorMatrix/FreeQuantDB/main/catalog/index.json)
