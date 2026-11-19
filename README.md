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

`fqdb gateway` 打开管理网页。业务命令和 Python 的 `import fqdb` 也会启动或连接同一个传输后台。Python 导入时不打开网页。后台恢复本地可用数据的做种并检查已保存的订阅，之后默认每 3 小时检查磁力目录。

在「传输 → 传输设置」选择数据集和频率后，可开启自动下载。仅更新磁力目录时可以留空。下载完成后会用原磁力继续做种。这里也可以暂停共享、设置下载和上传上限、添加优先 IP。

关闭网页、终端或 Python 脚本后，后台会继续处理订阅、下载和共享。运行 `fqdb service stop` 结束后台。下次使用业务命令或在新 Python 进程中导入 `fqdb` 时会再次启动。首次使用的总上传上限为 **500 KB/s（500,000 B/s）**。在页面中暂停共享或修改限速后，设置会保留。

## 数据目录

CLI 的 `--data-dir` 或 `-d` 会保存所选目录，之后 CLI 和 Python 共用该选择。未保存目录时默认使用 `~/.fqdb`。

```bash
fqdb gateway -d ~/fqdb_data
```

如果从 Python 首次使用自定义目录，可以在导入前为进程设置 `FQDB_DATA_DIR`，也可以调用 `fqdb.init(data_dir)` 切换并保存目录。切换时，明确使用旧目录的后台会保留，正在处理的任务可继续完成，CLI 和 Python 会提示旧后台的状态。已登记的数据目录或挂载不可用时，恢复原挂载后再使用，或显式选择其他目录。

## 帮助

```bash
python -m fqdb -h
python -m fqdb gateway -h
```

其他命令同样支持 `-h` 或 `--help`。

## Python

```python
import fqdb
```

可以用 `fqdb.provider.start()` 显式开启共享，或用 `fqdb.provider.stop()` 暂停。只需离线读取时，在导入前设置 `FQDB_AUTOSTART=0`。

## 文档与许可

[数据下载](catalog/README.md) · [管理网页](docs/DASHBOARD.md) · [数据说明](docs/MARKET_DATA.md) · [Python 与开发环境](docs/PYTHON_SUPPORT.md)

软件采用 [Apache-2.0](LICENSE) 许可。
