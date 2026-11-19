# FreeQuantDB

`fqdb` 是可通过命令行、管理网页或 Python 使用的本地量化数据管理包，面向已符合约定的 Parquet 文件。

## 安装

需要 Python 3.11 或更高版本，以及 Git。

```bash
python -m pip install git+https://github.com/FactorMatrix/FreeQuantDB.git
```

## 启动

```bash
python -m fqdb gateway
```

首次运行会初始化数据目录并启动独立的 BT 后台。Gateway 会恢复有原始种子信息的本地数据包，下载完成后也会用原磁力继续做种。关闭网页或按 `Ctrl+C` 退出管理界面后，下载和做种仍可继续。

在「传输 → 传输设置」可以暂停后台共享、设置下载和上传上限、添加优先 IP。暂停设置会保留。需要结束整个后台时，运行 `python -m fqdb service stop`。电脑重启后，再运行 Gateway 即可恢复共享。

默认数据目录是用户目录下的 `~/.fqdb`；现有环境变量和已保存的目录设置继续生效。Python 包安装在当前 Python 环境中。

可选：本次使用其他数据目录。

```bash
python -m fqdb gateway -d ~/fqdb_data
```

## 帮助

```bash
python -m fqdb -h
python -m fqdb gateway -h
```

其他命令同样支持 `-h` 或 `--help`。

## Python 直接做种

本地已有可提供的数据时，无需先打开网页：

```python
import fqdb

fqdb.provider.run()
```

前台运行，按 `Ctrl+C` 停止；普通 `import fqdb` 不会启动服务。命令行也可直接运行 `python -m fqdb provider run`。

## 文档与许可

[数据下载](catalog/README.md) · [管理网页](docs/DASHBOARD.md) · [数据说明](docs/MARKET_DATA.md) · [Python 与开发环境](docs/PYTHON_SUPPORT.md)

软件采用 [Apache-2.0](LICENSE) 许可。
