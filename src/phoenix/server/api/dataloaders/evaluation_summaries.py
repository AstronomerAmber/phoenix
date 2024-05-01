from collections import defaultdict
from datetime import datetime
from typing import (
    Any,
    AsyncContextManager,
    Callable,
    DefaultDict,
    List,
    Literal,
    Optional,
    Tuple,
)

import pandas as pd
from aioitertools.itertools import groupby
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.dataloader import AbstractCache, DataLoader
from typing_extensions import TypeAlias, assert_never

from phoenix.db import models
from phoenix.server.api.input_types.TimeRange import TimeRange
from phoenix.server.api.types.EvaluationSummary import EvaluationSummary
from phoenix.trace.dsl import SpanFilter

Kind: TypeAlias = Literal["span", "trace"]
ProjectRowId: TypeAlias = int
TimeInterval: TypeAlias = Tuple[Optional[datetime], Optional[datetime]]
FilterCondition: TypeAlias = Optional[str]
EvalName: TypeAlias = str

Segment: TypeAlias = Tuple[Kind, ProjectRowId, TimeInterval, FilterCondition]
Param: TypeAlias = EvalName

Key: TypeAlias = Tuple[Kind, ProjectRowId, Optional[TimeRange], FilterCondition, EvalName]
Result: TypeAlias = Optional[EvaluationSummary]
ResultPosition: TypeAlias = int
DEFAULT_VALUE: Result = None


def _cache_key_fn(key: Key) -> Tuple[Segment, Param]:
    kind, project_rowid, time_range, filter_condition, eval_name = key
    interval = (
        (time_range.start, time_range.end) if isinstance(time_range, TimeRange) else (None, None)
    )
    return (kind, project_rowid, interval, filter_condition), eval_name


class EvaluationSummaryDataLoader(DataLoader[Key, Result]):
    def __init__(
        self,
        db: Callable[[], AsyncContextManager[AsyncSession]],
        cache_map: Optional[AbstractCache[Key, Result]] = None,
    ) -> None:
        super().__init__(
            load_fn=self._load_fn,
            cache_key_fn=_cache_key_fn,
            cache_map=cache_map,
        )
        self._db = db

    async def _load_fn(self, keys: List[Key]) -> List[Result]:
        results: List[Result] = [DEFAULT_VALUE] * len(keys)
        arguments: DefaultDict[
            Segment,
            DefaultDict[Param, List[ResultPosition]],
        ] = defaultdict(lambda: defaultdict(list))
        for position, key in enumerate(keys):
            segment, param = _cache_key_fn(key)
            arguments[segment][param].append(position)
        for segment, params in arguments.items():
            stmt = _get_stmt(segment, *params.keys())
            async with self._db() as session:
                data = await session.stream(stmt)
                async for eval_name, group in groupby(data, lambda row: row.name):
                    summary = EvaluationSummary(pd.DataFrame(group))
                    for position in params[eval_name]:
                        results[position] = summary
        return results


def _get_stmt(
    segment: Segment,
    *eval_names: Param,
) -> Select[Any]:
    kind, project_rowid, (start_time, end_time), filter_condition = segment
    stmt = select()
    if kind == "span":
        msa = models.SpanAnnotation
        name_column, label_column, score_column = msa.name, msa.label, msa.score
        annotator_kind_column = msa.annotator_kind
        time_column = models.Span.start_time
        stmt = stmt.join(models.Span).join_from(models.Span, models.Trace)
        if filter_condition:
            sf = SpanFilter(filter_condition)
            stmt = sf(stmt)
    elif kind == "trace":
        mta = models.TraceAnnotation
        name_column, label_column, score_column = mta.name, mta.label, mta.score
        annotator_kind_column = mta.annotator_kind
        time_column = models.Trace.start_time
        stmt = stmt.join(models.Trace)
    else:
        assert_never(kind)
    stmt = stmt.add_columns(
        name_column,
        label_column,
        func.count().label("record_count"),
        func.count(label_column).label("label_count"),
        func.count(score_column).label("score_count"),
        func.sum(score_column).label("score_sum"),
    )
    stmt = stmt.group_by(name_column, label_column)
    stmt = stmt.order_by(name_column, label_column)
    stmt = stmt.where(models.Trace.project_rowid == project_rowid)
    stmt = stmt.where(annotator_kind_column == "LLM")
    stmt = stmt.where(or_(score_column.is_not(None), label_column.is_not(None)))
    stmt = stmt.where(name_column.in_(eval_names))
    if start_time:
        stmt = stmt.where(start_time <= time_column)
    if end_time:
        stmt = stmt.where(time_column < end_time)
    return stmt
