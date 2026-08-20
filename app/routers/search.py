from fastapi import APIRouter, HTTPException, Query, status

from app.schemas.search import CatalogDetailResponse, CatalogSearchResponse
from app.services.pricing.catalog_search_service import (
    CatalogItemNotFoundError,
    CatalogSearchError,
    CatalogSearchService,
)


router = APIRouter(prefix="/api/pricing/catalog", tags=["Catalog Search"])


@router.get("/search", response_model=CatalogSearchResponse)
async def search_catalog(
    q: str = Query("", min_length=0, max_length=120),
    limit: int = Query(20, ge=1, le=50),
    categoryGroup: str | None = Query(default=None, min_length=1, max_length=40),
    minPrice: float | None = Query(default=None, ge=0),
    maxPrice: float | None = Query(default=None, ge=0),
    source: str | None = Query(default=None, pattern="^(pricecharting|kicksdb)$"),
) -> CatalogSearchResponse:
    try:
        return CatalogSearchService().search(
            query=q,
            limit=limit,
            category_group=categoryGroup,
            min_price=minPrice,
            max_price=maxPrice,
            source=source,
        )
    except CatalogSearchError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "catalog_search_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error


@router.get("/{catalog_id}", response_model=CatalogDetailResponse)
async def get_catalog_detail(
    catalog_id: str,
    history_limit: int = Query(30, alias="historyLimit", ge=1, le=90),
    currency: str | None = Query(default=None, max_length=8),
) -> CatalogDetailResponse:
    try:
        return CatalogSearchService().detail(
            catalog_id=catalog_id,
            history_limit=history_limit,
            currency=currency,
        )
    except CatalogItemNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "catalog_item_not_found",
                "message": str(error),
                "retryable": False,
            },
        ) from error
    except CatalogSearchError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "catalog_search_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error
