from uuid import uuid4

from app.schemas.portfolio import PortfolioCreateRequest, PortfolioItem


class InMemoryPortfolioService:
    def __init__(self) -> None:
        self._items: dict[str, PortfolioItem] = {}

    def list_items(self) -> list[PortfolioItem]:
        return list(self._items.values())

    def get_item(self, item_id: str) -> PortfolioItem | None:
        return self._items.get(item_id)

    def update_item_data(self, item_id: str, data: dict) -> PortfolioItem | None:
        item = self._items.get(item_id)
        if item is None:
            return None
        item.data.update(data)
        self._items[item_id] = item
        return item

    def add_item(self, request: PortfolioCreateRequest) -> PortfolioItem:
        item_id = request.id or str(uuid4())
        item = PortfolioItem(id=item_id, data=request.data)
        self._items[item_id] = item
        return item

    def delete_item(self, item_id: str) -> bool:
        return self._items.pop(item_id, None) is not None


portfolio_service = InMemoryPortfolioService()
