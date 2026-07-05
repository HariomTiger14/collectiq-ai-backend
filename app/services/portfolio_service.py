from uuid import uuid4

from app.schemas.portfolio import PortfolioCreateRequest, PortfolioItem


class InMemoryPortfolioService:
    def __init__(self) -> None:
        self._items: dict[str, PortfolioItem] = {}

    def list_items(self) -> list[PortfolioItem]:
        return list(self._items.values())

    def add_item(self, request: PortfolioCreateRequest) -> PortfolioItem:
        item_id = request.id or str(uuid4())
        item = PortfolioItem(id=item_id, data=request.data)
        self._items[item_id] = item
        return item

    def delete_item(self, item_id: str) -> bool:
        return self._items.pop(item_id, None) is not None


portfolio_service = InMemoryPortfolioService()
