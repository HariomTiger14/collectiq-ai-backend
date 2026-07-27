from fastapi import APIRouter, HTTPException, Query, status

from app.core.config import settings
from app.schemas.metadata import EbayMetadataSearchResponse
from app.services.metadata.ebay_metadata_provider import (
    EbayMetadataError,
    EbayMetadataProvider,
    EbayMetadataRateLimitError,
    EbayMetadataTimeoutError,
    EbayMetadataUnavailableError,
)


router = APIRouter(prefix="/api/metadata", tags=["Metadata"])


@router.get("/ebay/search", response_model=EbayMetadataSearchResponse)
async def search_ebay_metadata(
    q: str = Query("", min_length=0, max_length=120),
    limit: int = Query(10, ge=1, le=20),
) -> EbayMetadataSearchResponse:
    try:
        return _provider().search(q, limit)
    except EbayMetadataRateLimitError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "ebay_metadata_rate_limited",
                "message": str(error),
                "retryable": True,
            },
        ) from error
    except EbayMetadataTimeoutError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "ebay_metadata_timeout",
                "message": str(error),
                "retryable": True,
            },
        ) from error
    except EbayMetadataUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "ebay_metadata_unavailable",
                "message": str(error),
                "retryable": True,
            },
        ) from error
    except EbayMetadataError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "ebay_metadata_failed",
                "message": str(error),
                "retryable": True,
            },
        ) from error


def _provider() -> EbayMetadataProvider:
    return EbayMetadataProvider(
        access_token=settings.ebay_access_token,
        client_id=settings.ebay_client_id,
        client_secret=settings.ebay_client_secret,
        oauth_token_url=settings.ebay_oauth_token_url,
        oauth_scope=settings.ebay_oauth_scope,
        browse_api_url=settings.ebay_browse_api_url,
        marketplace_id=settings.ebay_marketplace_id,
        timeout_seconds=settings.ebay_timeout_seconds,
    )

