# CollectIQ AI Backend

Production-ready FastAPI foundation for the CollectIQ AI backend.

## Features

- Health check endpoint
- Mock scanner analysis endpoint
- In-memory portfolio endpoints
- Clean Architecture style separation between routers, schemas, services, repositories, and database packages
- Typed Pydantic request and response models

## Project Structure

```text
app/
  main.py
  core/
    config.py
    dependencies.py
  routers/
    health.py
    scanner.py
    portfolio.py
  schemas/
    scan_request.py
    scan_response.py
    portfolio.py
  services/
    ai_service.py
    image_service.py
    portfolio_service.py
  repositories/
  database/
requirements.txt
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload
```

## Endpoints

### `GET /health`

Returns:

```json
{
  "status": "ok",
  "version": "1.0"
}
```

### `POST /scanner/analyze`

Accepts `multipart/form-data` with an `image` field.

Supported image types:

- `jpg`
- `jpeg`
- `png`

Maximum upload size: `10MB`.

Uploaded images are saved to `uploads/` with UUID filenames and are served from `/uploads/{filename}`.

### `GET /portfolio`

Returns all in-memory portfolio items.

### `POST /portfolio`

Stores a portfolio item in memory.

### `DELETE /portfolio/{id}`

Deletes an in-memory portfolio item by id.
