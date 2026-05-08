from pydantic import BaseModel, ConfigDict, HttpUrl


class ScrapeRequest(BaseModel):
    url: HttpUrl


class ScrapeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    title: str
    price: int
    in_stock: bool
    image_url: str | None
