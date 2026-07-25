from fastapi import APIRouter, HTTPException, Query, status

from app.schemas.search import CatalogSearchResponse
from app.services.pricing.catalog_search_service import (
    CatalogSearchError,
    CatalogSearchService,
)


router = APIRouter(prefix="/api/pricing/catalog", tags=["Catalog Search"])


@router.get("/search", response_model=CatalogSearchResponse)
async def search_catalog(
    q: str = Query("", min_length=0, max_length=120),
    limit: int = Query(20, ge=1, le=50),
) -> CatalogSearchResponse:
    try:
        return CatalogSearchService().search(query=q, limit=limit)
    except CatalogSearchError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "catalog_search_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error
