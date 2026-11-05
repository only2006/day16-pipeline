"""价格表与成本计算。

⚠️ 单价以各平台官网为准，这里只是示例值。
   平台调价后，改这张表就行，不用改业务代码。

注意：输入 / 输出单价不同（输出通常贵 3–4 倍），所以这里和 Day16 README 里
「统一按 1.25–2.5 元 / 百万 token」的粗估口径不可直接比较 —— Day17 起改为
精确分项计费，数字会比粗估偏高，属正常。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input_per_m: float      # 元 / 百万 token
    output_per_m: float     # 元 / 百万 token


PRICING: dict[str, Price] = {
    # 名字要和 API 里的 model 参数完全一致
    "deepseek-chat": Price(2.0, 8.0),
    "deepseek-reasoner": Price(4.0, 16.0),
    # 便宜的小模型，用来做分类 / 抽取这类简单活
    "qwen-turbo": Price(0.3, 0.6),
}

DEFAULT_PRICE = Price(2.0, 8.0)


def price_of(model: str) -> Price:
    return PRICING.get(model, DEFAULT_PRICE)


def cost_of(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """返回人民币成本。"""
    p = price_of(model)
    return (prompt_tokens * p.input_per_m + completion_tokens * p.output_per_m) / 1_000_000
