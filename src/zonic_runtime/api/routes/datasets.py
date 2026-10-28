"""``/v1/datasets``: a page of a dataset the caller's runs produced (P3-04, P3-05)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from zonic_runtime.api.deps import current_caller, deps_of
from zonic_runtime.api.errors import ERROR_RESPONSES
from zonic_runtime.application.caller import Caller
from zonic_runtime.models.api.responses import DatasetPage

router = APIRouter(tags=["datasets"], responses=ERROR_RESPONSES)
CallerDep = Annotated[Caller, Depends(current_caller)]


@router.get(
    "/datasets/{datasetId}",
    operation_id="getDataset",
    summary="A page of a dataset's rows, filtered and sorted",
    response_model=DatasetPage,
    response_model_exclude_none=True,
)
async def get_dataset(
    request: Request,
    caller: CallerDep,
    dataset_id: Annotated[str, Path(alias="datasetId", max_length=100)],
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int | None, Query(alias="pageSize", ge=1)] = None,
    sort: Annotated[str | None, Query(max_length=200)] = None,
    filter: Annotated[str | None, Query(max_length=2000)] = None,
) -> DatasetPage:
    return await deps_of(request).datasets.page(
        caller, dataset_id, page=page, page_size=page_size, sort=sort, filter=filter
    )
