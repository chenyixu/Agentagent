"""中文分词。

设计稿 §10.1：中文词法检索必须选择合适的分词/检索方案，不能把 PostgreSQL 默认
英文全文检索直接当成中文 BM25。首版小语料在租户范围内使用应用内中文分词与
关键词评分，并维护"肩颈、开背、项目别名、技师名"等测试词表，报告分词错误造成
的召回损失。

实现选择：**词典切分 + 字符二元组回退**。

- 词典来自业务域词表（服务别名、门店术语、常见护理词）；
- 二元组保证未登录词（人名、错别字、新词）仍能被召回，代价是索引更大；
- 不引入需要编译的第三方分词库，保证无网络/无编译环境下也能复现检索结果。
"""

from __future__ import annotations

import re
from typing import Iterable

#: 业务域词表。词典词优先切分，未命中部分走二元组回退。
DOMAIN_LEXICON: tuple[str, ...] = (
    # 服务与项目
    "肩颈舒缓", "肩颈", "开背理疗", "开背", "理疗", "推拿", "按摩", "足疗",
    "精油", "力度", "时长", "项目", "服务",
    # 预约语义
    "预约", "改约", "取消", "占位", "确认", "时段", "提前", "迟到", "爽约",
    "到店", "前台", "门店", "技师", "房间", "排班", "营业", "价格", "费用",
    "免费", "窗口", "小时", "分钟", "今天", "明天", "后天", "周末",
    # 护理与注意
    "孕妇", "孕期", "皮肤", "破损", "损伤", "急性", "饮水", "换衣", "衣物",
    "颈椎", "腰椎", "久坐", "头痛", "疲劳", "放松",
)

#: 停用词。检索时丢弃，避免"的/了/吗"这类高频词主导评分。
STOPWORDS: frozenset[str] = frozenset(
    {
        "的", "了", "是", "在", "我", "你", "他", "她", "它", "们", "和", "与",
        "或", "吗", "呢", "吧", "啊", "呀", "就", "都", "也", "很", "有", "没有",
        "个", "这", "那", "请问", "一下", "可以", "能", "要", "会", "把", "被",
        "对", "从", "到", "在", "为", "以", "及", "等", "着", "过", "给", "还",
    }
)

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[a-zA-Z0-9]+")

#: 词典按长度倒序，保证"肩颈舒缓"先于"肩颈"匹配。
_LEXICON_SORTED: tuple[str, ...] = tuple(
    sorted(DOMAIN_LEXICON, key=len, reverse=True)
)
_MAX_LEXICON_LEN = max(len(word) for word in _LEXICON_SORTED)


def _segment_cjk(segment: str) -> list[str]:
    """词典优先 + 二元组回退的前向最大匹配。"""

    tokens: list[str] = []
    index = 0
    length = len(segment)
    while index < length:
        matched = ""
        for size in range(min(_MAX_LEXICON_LEN, length - index), 1, -1):
            candidate = segment[index : index + size]
            if candidate in DOMAIN_LEXICON:
                matched = candidate
                break
        if matched:
            tokens.append(matched)
            index += len(matched)
            continue
        # 单字不进词典，但作为二元组的一部分保留上下文。
        if index + 1 < length:
            tokens.append(segment[index : index + 2])
        index += 1
    return tokens


def tokenize(text: str) -> list[str]:
    """把文本切成检索词元。

    返回保留顺序的词元，包含词典词、二元组与拉丁词；停用词已剔除。
    """

    if not text:
        return []
    tokens: list[str] = []
    for chunk in _CJK.findall(text):
        tokens.extend(_segment_cjk(chunk))
    tokens.extend(match.lower() for match in _LATIN.findall(text))
    return [token for token in tokens if token and token not in STOPWORDS]


def unique_tokens(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for token in tokenize(text):
        seen.setdefault(token, None)
    return list(seen)


def token_coverage(query_tokens: Iterable[str], document_tokens: Iterable[str]) -> tuple[int, int]:
    """返回 (命中词数, 查询词数)。用于解释检索为什么给出这条证据。"""

    query_list = [t for t in query_tokens]
    document_set = set(document_tokens)
    if not query_list:
        return 0, 0
    hits = sum(1 for token in query_list if token in document_set)
    return hits, len(query_list)
