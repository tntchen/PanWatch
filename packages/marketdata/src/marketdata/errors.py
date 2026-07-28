"""marketdata 异常类型。"""


class MarketDataError(Exception):
    """本包所有异常的基类。"""


class VendorError(MarketDataError):
    """单个 vendor 抓取失败(Engine 捕获后转移到下一个源)。"""


class ConfigError(VendorError):
    """vendor 配置类异常(token 缺失/未授权/依赖未安装)。

    与传输/服务异常分开归类(R2,docs/29 §R2-3):配置异常不计入熔断的
    连续运输失败计数,避免"忘了配 token"把源熔断掉;Engine/指标层以
    error_class="config" 单列,UI/日志可与网络故障区分。
    """
