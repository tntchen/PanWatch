"""应用镜像必须安装 Tushare；vendor 的惰性导入仅用于独立包的优雅降级。"""

import importlib.util


def test_tushare_is_installed_as_application_runtime_dependency() -> None:
    assert importlib.util.find_spec("tushare") is not None
