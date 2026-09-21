"""展示正文选择（GET /messages/{id}/display-body）。

在已归档的 MIME 节点表（``database.list_nodes`` 的结果）上模拟
邮件客户端“正文应该显示哪一部分”的决策，纯查询、不改写任何记录。
递归规则：

* ``attachment`` disposition 的子树、``message/rfc822`` /
  ``message/global`` 封装报文的内部正文一律不作候选；
* ``multipart/alternative``：在可展示分支中优先与 ``prefer``
  （html→text/html、plain→text/plain）匹配者（按文档序取第一个
  匹配），都不匹配时取 **最后一个可展示分支**（RFC 2046 的
  “越靠后越接近原文”语义）；
* ``multipart/related``（RFC 2387）：``start`` 参数按 Content-ID
  匹配唯一直接子件；未配置（或配置但未命中）时取 **首个子件**；
  ``start`` 命中多个子件属于归档数据歧义，抛
  :class:`DisplayBodyError`（HTTP 422，定位到该 related 节点）；
* ``multipart/mixed`` 及其余未特化的 multipart 子类型
  （signed、report、digest 等）：取 **首个可展示子件**
  （RFC 2046：未知子类型按 mixed 处理）。

“可展示”指递归选择最终能得到一个 text/* 叶子（非附件、不在
message/rfc822 内部）。选择结果附带逐层轨迹（每个经过的容器一条：
规则、候选、胜出子件），便于审计与调试。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from .errors import DisplayBodyError, MIMEParseError
from .mimeparser import parse_structured_params
from .related import json_loads_headers, normalize_content_id

# prefer 参数到媒体类型的映射
PREFER_MEDIA_TYPES = {"html": "text/html", "plain": "text/plain"}


@dataclass
class DisplayBodyCandidate:
    """递归选择的结果：一个可展示的 text/* 叶子。"""

    node_path: str
    content_type: str
    text: str


@dataclass
class TraceEntry:
    """一层容器（或最终叶子）的选择轨迹。"""

    node_path: str
    content_type: str
    rule: str
    chosen: Optional[str] = None  # 选中的子件路径；叶子为 None
    candidates: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


def select_display_body(
    rows: list[sqlite3.Row], prefer: str
) -> Optional[tuple[DisplayBodyCandidate, list[TraceEntry]]]:
    """在一封邮件的节点表上选择展示正文。

    ``rows`` 为 ``database.list_nodes`` 的结果（含根节点 ``""``）。
    返回 ``(候选叶子, 逐层轨迹)``；没有可展示正文时返回 ``None``。
    ``start`` 重复命中时抛 :class:`DisplayBodyError`。
    """

    by_path = {r["path"]: r for r in rows}
    root = by_path.get("")
    if root is None:  # 防御：正常数据必然有根
        return None
    return _select(root, by_path, prefer)


def _select(
    row: sqlite3.Row,
    by_path: dict[str, sqlite3.Row],
    prefer: str,
) -> Optional[tuple[DisplayBodyCandidate, list[TraceEntry]]]:
    # attachment 子树整体排除（disposition 入库时已小写）
    if row["disposition"] == "attachment":
        return None

    kind = row["kind"]
    if kind == "text":
        candidate = DisplayBodyCandidate(
            node_path=row["path"],
            content_type=row["content_type"],
            text=row["text_content"] or "",
        )
        return candidate, [
            TraceEntry(row["path"], row["content_type"], "leaf")
        ]
    if kind in ("blob", "message"):
        # 二进制叶子无法作为文本正文；message/rfc822 内部正文不作候选
        return None

    children = [by_path[p] for p in json.loads(row["children_json"])]
    content_type = row["content_type"]
    if content_type == "multipart/alternative":
        return _select_alternative(row, children, by_path, prefer)
    if content_type == "multipart/related":
        return _select_related(row, children, by_path, prefer)
    # multipart/mixed 及其余子类型（RFC 2046：未知按 mixed 处理）
    return _select_first_displayable(row, children, by_path, prefer)


def _displayable_children(
    children: list[sqlite3.Row],
    by_path: dict[str, sqlite3.Row],
    prefer: str,
) -> list[tuple[sqlite3.Row, tuple[DisplayBodyCandidate, list[TraceEntry]]]]:
    usable = []
    for child in children:
        result = _select(child, by_path, prefer)
        if result is not None:
            usable.append((child, result))
    return usable


def _select_first_displayable(
    row: sqlite3.Row,
    children: list[sqlite3.Row],
    by_path: dict[str, sqlite3.Row],
    prefer: str,
) -> Optional[tuple[DisplayBodyCandidate, list[TraceEntry]]]:
    usable = _displayable_children(children, by_path, prefer)
    if not usable:
        return None
    chosen_row, (candidate, sub_trace) = usable[0]
    entry = TraceEntry(
        node_path=row["path"],
        content_type=row["content_type"],
        rule="mixed.first_displayable",
        chosen=chosen_row["path"],
        candidates=[child["path"] for child, _ in usable],
    )
    return candidate, [entry, *sub_trace]


def _select_alternative(
    row: sqlite3.Row,
    children: list[sqlite3.Row],
    by_path: dict[str, sqlite3.Row],
    prefer: str,
) -> Optional[tuple[DisplayBodyCandidate, list[TraceEntry]]]:
    usable = _displayable_children(children, by_path, prefer)
    if not usable:
        return None
    prefer_type = PREFER_MEDIA_TYPES[prefer]
    matching = [
        (child, result)
        for child, result in usable
        if result[0].content_type == prefer_type
    ]
    if matching:
        chosen_row, (candidate, sub_trace) = matching[0]
        rule = "alternative.prefer"
    else:
        chosen_row, (candidate, sub_trace) = usable[-1]
        rule = "alternative.last_displayable"
    entry = TraceEntry(
        node_path=row["path"],
        content_type=row["content_type"],
        rule=rule,
        chosen=chosen_row["path"],
        candidates=[child["path"] for child, _ in usable],
        detail={"prefer": prefer},
    )
    return candidate, [entry, *sub_trace]


def _select_related(
    row: sqlite3.Row,
    children: list[sqlite3.Row],
    by_path: dict[str, sqlite3.Row],
    prefer: str,
) -> Optional[tuple[DisplayBodyCandidate, list[TraceEntry]]]:
    if not children:  # 防御：multipart 至少有一个子件
        return None
    start = _related_start_param(row)
    chosen_row: Optional[sqlite3.Row] = None
    detail: dict = {}
    if start:
        target = normalize_content_id(start)
        matches = [
            child
            for child in children
            if target and target in _content_ids_of(child)
        ]
        if len(matches) > 1:
            raise DisplayBodyError(
                "AMBIGUOUS_RELATED_START",
                f"multipart/related 的 start 参数 {start!r} 同时命中 "
                f"{len(matches)} 个子件，无法确定根部分",
                row["path"],
                {"start": start, "candidates": [c["path"] for c in matches]},
            )
        if matches:
            chosen_row = matches[0]
            rule = "related.start"
            detail = {"start": start}
        else:
            # start 未命中：按未配置处理，回退首个子件
            rule = "related.first"
            detail = {"start": start, "start_matched": False}
    else:
        rule = "related.first"
    if chosen_row is None:
        chosen_row = children[0]
    result = _select(chosen_row, by_path, prefer)
    if result is None:
        return None
    candidate, sub_trace = result
    entry = TraceEntry(
        node_path=row["path"],
        content_type=row["content_type"],
        rule=rule,
        chosen=chosen_row["path"],
        candidates=[c["path"] for c in children],
        detail=detail,
    )
    return candidate, [entry, *sub_trace]


def _related_start_param(row: sqlite3.Row) -> Optional[str]:
    """从存储的 Content-Type 头中取出 start 参数（容错）。"""

    for name, value in json_loads_headers(row["headers_json"]):
        if name.lower() != "content-type":
            continue
        try:
            params = parse_structured_params(
                value, path=row["path"], header_name="Content-Type"
            )
        except MIMEParseError:
            return None  # 旧数据/脏数据：按未配置处理
        return params.get("start") or None
    return None


def _content_ids_of(row: sqlite3.Row) -> list[str]:
    ids: list[str] = []
    for name, value in json_loads_headers(row["headers_json"]):
        if name.lower() == "content-id":
            cid = normalize_content_id(value)
            if cid and cid not in ids:
                ids.append(cid)
    return ids
