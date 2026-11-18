# Python支持与开发环境

目标为标准CPython **3.11及以上**，不设置固定上限。主开发、测试和本机服务使用独立的 `.venvs/FQDB` / CPython3.11；其他解释器只用于兼容验证。包、导入与命令统一为 `fqdb`。

```bash
python3.11 -m venv --prompt FQDB .venvs/FQDB
source .venvs/FQDB/bin/activate
python -m pip install '.[dev]'
```

Python 包依赖 libtorrent、PyArrow 和 Pandas 等组件。缺少 libtorrent 绑定时，可通过 `scripts/build_libtorrent.py` 构建。[官方绑定文档](https://libtorrent.org/python_binding.html)

使用仓库外的独立目录运行合成数据测试。

```bash
FQDB_TEST_DATA_ROOT=/path/to/fqdb-tests python -m pytest -q
python -m ruff check src tests examples scripts
```

安装 wheel 后，可以用 `python scripts/check_python_install.py -d /path/to/fqdb-install-check` 验证已安装的 SDK、CLI 和网页。测试目录须为空或尚未创建。

新增命令、服务动作、页面或资产时，请查阅[架构与扩展入口](ARCHITECTURE.md)。
