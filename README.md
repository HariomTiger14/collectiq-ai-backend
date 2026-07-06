# CollectIQ AI Backend

FastAPI backend for CollectIQ AI analysis, portfolio, and future provider
integrations.

## Current MVP

- `GET /health`
- `POST /scanner/analyze` for multipart scanner uploads
- `POST /api/analyze` for the Flutter backend AI contract
- Mock AI recognition
- Backend pricing provider pipeline with mock fallback

The backend is the only place future OpenAI, Gemini, pricing, marketplace, or
payment provider keys should live. Flutter must call this backend/proxy only.
`AI_PROVIDER=mock` is the default, so `/api/analyze` returns mock analysis unless
OpenAI is explicitly selected in backend `.env`.

## Local Setup

From the repository root:

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Android emulator Flutter config for future backend testing:

```powershell
flutter run `
  --dart-define=AI_ANALYSIS_PROVIDER=openai_vision `
  --dart-define=AI_BACKEND_ANALYSIS_ENDPOINT_URL=http://10.0.2.2:8000/api/analyze
```

Physical Android device on the same network:

```powershell
flutter run `
  --dart-define=AI_ANALYSIS_PROVIDER=openai_vision `
  --dart-define=AI_BACKEND_ANALYSIS_ENDPOINT_URL=http://YOUR_LAN_IP:8000/api/analyze
```

Mock mode remains the Flutter default and makes no backend AI calls.

## Environment

Copy `.env.example` to `.env`.

```text
AI_PROVIDER=mock
PRICING_PROVIDER=mock
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
GEMINI_TIMEOUT_SECONDS=30
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
OPENAI_TIMEOUT_SECONDS=30
EBAY_ACCESS_TOKEN=
EBAY_BROWSE_API_URL=https://api.ebay.com/buy/browse/v1/item_summary/search
EBAY_MARKETPLACE_ID=EBAY_AU
EBAY_TIMEOUT_SECONDS=10
TCGPLAYER_CLIENT_ID=
TCGPLAYER_CLIENT_SECRET=
TCGPLAYER_API_BASE=https://api.tcgplayer.com
TCGPLAYER_TIMEOUT_SECONDS=10
PRICECHARTING_API_KEY=
PRICECHARTING_API_BASE=https://www.pricecharting.com
PRICECHARTING_TIMEOUT_SECONDS=10
PRICING_CACHE_TTL_SECONDS=900
PRICING_PROVIDER_MIN_INTERVAL_MS=250
```

For v1.0 preparation, keep `AI_PROVIDER=mock` until the production backend AI
provider is intentionally enabled. For SIT real analysis, use `AI_PROVIDER=auto`
with `GEMINI_API_KEY` configured server-side. `OPENAI_API_KEY` can remain
configured as a backup provider. Do not commit real secrets.

## POST /api/analyze

This endpoint accepts the JSON envelope prepared by Flutter's backend client:

```json
{
  "request": {
    "imagePath": "/local/app/path/card.jpg",
    "imageSource": "camera",
    "requestedCategory": "Pokemon Card",
    "appVersion": "1.0.0",
    "deviceMetadata": {},
    "timestamp": "2026-06-30T09:00:00Z"
  },
  "image": {
    "fileName": "card.jpg",
    "mimeType": "image/jpeg",
    "sizeBytes": 123456,
    "imageSource": "camera",
    "localFilePath": "/local/app/path/card.jpg"
  }
}
```

The MVP returns mock recognition and mock pricing data shaped like Flutter's
`AiBackendAnalysisResponse`.

Provider selection:

- `AI_PROVIDER=mock`: default, no external calls.
- `AI_PROVIDER=auto`: tries Gemini first, then OpenAI, then deterministic mock
  fallback.
- `AI_PROVIDER=gemini`: tries Gemini Vision first, then deterministic mock
  fallback if unavailable.
- `AI_PROVIDER=openai`: tries OpenAI Vision first, then deterministic mock
  fallback if unavailable.

Keep `GEMINI_API_KEY` and `OPENAI_API_KEY` server-side in `backend/.env` or the
hosting provider secret store only.

Pricing selection:

- `PRICING_PROVIDER=mock`: default deterministic pricing and comparable sales.
- `PRICING_PROVIDER=ebay`: backend-only eBay Browse API provider.
- `PRICING_PROVIDER=tcgplayer`: backend-only TCGPlayer card pricing provider.
- `PRICING_PROVIDER=pricecharting`: backend-only PriceCharting guide pricing
  provider.
- `PRICING_PROVIDER=aggregate`: blends available eBay, TCGPlayer, and
  PriceCharting provider data, with mock fallback when live providers fail.

The eBay provider requires `EBAY_ACCESS_TOKEN` in backend `.env`. If the token
is missing, expired, rate-limited, or the provider fails, the aggregation service
falls back to deterministic mock pricing. Flutter still receives the same
response contract, and no third-party pricing API is called from the mobile app.
The TCGPlayer provider requires `TCGPLAYER_CLIENT_ID` and
`TCGPLAYER_CLIENT_SECRET` in backend `.env`. OAuth tokens are requested and
refreshed server-side only. Flutter never receives TCGPlayer credentials or
tokens. The PriceCharting provider requires `PRICECHARTING_API_KEY` in backend
`.env` and normalizes guide prices into the existing Flutter pricing contract.

When OpenAI is enabled, the backend sends the image to OpenAI's Responses API
with a strict structured-output schema. The prompt asks for collectible
identification, conservative AUD valuation, confidence explanation, detection
quality, reasoning, exactly three alternatives, and rich profile metadata. The
backend then normalizes that provider response into Flutter's
`AiBackendAnalysisResponse` contract.

The prompt also asks OpenAI to:

- identify item name, franchise/brand, category, set/series, year, visible
  number, manufacturer/publisher, language, edition/variant, and raw grading
  likelihood;
- return a 0-100 confidence score for each extracted field;
- avoid inventing data and use `null` or `Unknown` when details are not visible;
- classify confidence as `High` (90-100), `Medium` (70-89), or `Low` (<70);
- report low-confidence reasons and missing visual evidence;
- identify image-quality issues such as blur, glare/reflections, cropped edges,
  dark image, low resolution, and multiple collectibles in one photo;
- return actionable scan recommendations.

### Enable Real AI Locally

Gemini and OpenAI are opt-in and may incur API cost. Keep mock mode for normal
development and tests.

1. Copy `.env.example` to `.env`.
2. Set:

```text
AI_PROVIDER=auto
GEMINI_API_KEY=<server-side-gemini-key>
GEMINI_MODEL=gemini-2.5-flash
GEMINI_TIMEOUT_SECONDS=30
OPENAI_API_KEY=<server-side-openai-key>
OPENAI_MODEL=gpt-4.1-mini
OPENAI_TIMEOUT_SECONDS=30
```

3. Start the backend:

```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Never pass `GEMINI_API_KEY` or `OPENAI_API_KEY` to Flutter with `--dart-define`,
local storage, or source code. Flutter should only know the CollectIQ backend URL.

### Confidence Interpretation

- `High`: 90-100. The primary identity is likely, but users should still verify
  high-value items manually.
- `Medium`: 70-89. The broad item family is plausible, but details such as set,
  card number, issue, mint mark, or edition may need another scan.
- `Low`: below 70. Treat the result as a suggestion; retake the image or inspect
  manually before saving or valuing the item.

`fieldConfidence` reports confidence for individual fields, so a result may have
a high category match but lower confidence for year, number, edition, or grade.

### Image Capture Best Practices

- Use bright, even lighting.
- Avoid glare, sleeves with reflections, and dark backgrounds.
- Keep the full collectible inside the frame.
- Capture one collectible at a time.
- Retake if small text, set symbols, mint marks, issue numbers, or card numbers
  are blurry.
- Use a second close-up image later when adding multi-image analysis.

### Known AI Limitations

- AI estimates are not authentication, grading, or appraisal.
- Small print may be unreadable from normal phone distance.
- Reprints, variants, language differences, and editions can look very similar.
- Market value uses deterministic backend mock/fallback pricing until real
  pricing provider credentials and API mappings are enabled.
- Low-quality images should produce lower confidence and scan guidance rather
  than invented details.

### Prompt Engineering Guidelines

- Prefer conservative identification over overconfident claims.
- Ask for `null`/`Unknown` for unseen fields.
- Keep schema stable for Flutter.
- Add category-specific examples only when they improve structured output.
- Do not log full prompts or image payloads in production.
- Use `backend/tests/test_assets/manifest.json` as the golden dataset manifest.
  Current rows are placeholders until licensed or user-owned images are added.

## Pricing Architecture

Pricing is backend-only. Flutter sends images to the CollectIQ backend, the AI
provider identifies the collectible, and the backend pricing pipeline enriches
the result before returning the existing Flutter contract.

The pricing layer contains:

- `PricingProvider`: interface for provider implementations.
- `MockPricingProvider`: deterministic default used in tests and local dev.
- `EbayPricingProvider`: backend-only eBay Browse API integration.
- `TCGPlayerPricingProvider`: backend-only TCGPlayer OAuth/search/pricing
  integration for trading-card market prices.
- `PriceChartingPricingProvider`: backend-only PriceCharting guide pricing
  integration.
- `PricingAggregationService`: normalizes comparable sales, removes obvious
  outliers, calculates estimated value/range/confidence/trend, and falls back
  safely when providers are unavailable.
- `PricingConfidenceEngine`: turns raw comps into explainable confidence,
  outlier filtering, trend labels, comparable quality, and price explanations.

Aggregation calculates:

- `estimatedValue`
- `lowEstimate`
- `highEstimate`
- `pricingConfidence`
- `marketTrend`
- `sourceCount`
- `pricingAge`
- recent comparable sales

### Pricing Methodology

CollectIQ uses robust, explainable pricing rather than trusting a single raw
listing. The aggregation service first normalizes provider comps into
`MarketComparableSale`, then the pricing intelligence engine evaluates:

- provider count and provider agreement;
- number of comparable sales;
- price variance;
- market freshness/listing age;
- comparable quality;
- outliers removed from the estimate.

The displayed estimated value is the median of cleaned comparable sales. Median
is preferred over raw average because it is harder for one unusually high or low
listing to distort the valuation. Diagnostics also include a trimmed mean for
debugging and future charting.

### Confidence Calculation

Pricing confidence is scored from 0-100 using:

- provider count: multiple independent sources improve confidence;
- comparable count: more usable comps improve confidence;
- provider agreement: provider median prices close together improve confidence;
- variance: wide price spread lowers confidence;
- market freshness: recent comps score higher than stale comps;
- comparable quality: stronger title, set, card number, condition, and grading
  matches improve confidence;
- fallback penalty: mock fallback caps confidence lower than live provider data.

The backend diagnostics include the confidence reason, for example:

```text
12 comparable sales, 2 providers, 9% price variance, 94% provider agreement,
fresh market data
```

### Outlier Handling

Outlier detection removes broken and suspicious comps before estimating value:

- zero or negative prices;
- missing title/source rows;
- extremely low prices compared with median;
- extremely high prices compared with median;
- IQR-based extremes when enough comps are available.

The service keeps diagnostics for `outliersRemoved`, `medianPrice`,
`trimmedMean`, `priceVariance`, and `comparableCount`.

### Trend Classification

Comparable sales are sorted by sold date and compared across older vs recent
sales. The backend returns:

- `Strong Uptrend`
- `Moderate Uptrend`
- `Stable`
- `Moderate Downtrend`
- `Strong Downtrend`

### Comparable Quality

Each comparable is scored as:

- `Excellent`
- `Good`
- `Fair`
- `Poor`

Quality factors include title-token match, card number, set, condition, and
grading keywords such as PSA/BGS/CGC. Aggregated quality counts are included in
diagnostics.

### Explain Price

The pricing engine produces a plain-English explanation in diagnostics:

```text
Estimated value is based on 12 comparable sales, 2 providers, fresh market
data, 88% confidence, and eBay + TCGPlayer agreement at 94%.
```

This explanation is backend-generated and does not require any Flutter contract
changes.

### Provider Weighting

The current MVP treats cleaned comparable sales equally after provider-specific
normalization. Provider agreement and comparable quality influence confidence,
not the median value directly. Future providers can add weighting once real
validation data proves a provider should carry more or less influence for a
category.

### Pricing Error Handling

Provider failures should never crash analysis. The pipeline handles:

- provider unavailable
- timeout
- empty market data
- unsupported or incomplete provider data
- future rate-limit errors

If configured providers fail, the backend returns deterministic mock fallback
pricing with diagnostics marking `fallbackUsed=true`.

For eBay specifically:

- `EBAY_ACCESS_TOKEN` missing or invalid config -> fallback to mock.
- HTTP 429 -> rate-limit error -> fallback to mock.
- network timeout -> timeout error -> fallback to mock.
- empty or malformed pricing data -> fallback to mock.
- valid responses are normalized into comparable sales and aggregated into the
  existing Flutter market summary fields.

For TCGPlayer specifically:

- `TCGPLAYER_CLIENT_ID`/`TCGPLAYER_CLIENT_SECRET` missing -> fallback to mock.
- OAuth token request and refresh happen server-side only.
- HTTP 401 during search/pricing -> token refresh and one retry.
- HTTP 404 or empty product/pricing rows -> empty market data -> fallback.
- HTTP 429 -> rate-limit error -> fallback to mock.
- network timeout -> timeout error -> fallback to mock.
- valid product/pricing responses are normalized into comparable sales and the
  existing Flutter market summary fields.

For PriceCharting specifically:

- `PRICECHARTING_API_KEY` missing or invalid config -> fallback to mock.
- HTTP 401/403 -> authorization error -> fallback to mock.
- HTTP 404 or empty product/price rows -> empty market data -> fallback.
- HTTP 429 -> rate-limit error -> fallback to mock.
- network timeout -> timeout error -> fallback to mock.
- malformed pricing data -> fallback to mock.
- valid guide-price responses are normalized into comparable sales and the
  existing Flutter market summary fields.

### Pricing Diagnostics

Debug logs include provider count, response time, fallback usage, cache status,
pricing source count, provider name, pricing freshness, fallback reason,
provider agreement, variance, median value, outliers removed, comparable count,
confidence calculation, comparable quality, and price explanation. Logs must not
include provider API keys, payment tokens, or raw third-party credentials.

### Future Cache Strategy

The first pass uses an in-memory cache keyed by normalized collectible identity
fields such as category, title, year, set, card number, grade/condition,
language, and edition. Configure with:

```text
PRICING_CACHE_TTL_SECONDS=900
```

Recommended production evolution:

- short TTL for marketplace sold comps;
- longer TTL for guide prices;
- stale-while-revalidate for repeated scans;
- per-provider cache status in diagnostics.
- shared Redis/database cache if running multiple backend instances.

### Future Rate Limiting Strategy

Real pricing providers should be protected by:

- server-side API keys only;
- per-provider timeout budgets;
- local provider throttling via `PRICING_PROVIDER_MIN_INTERVAL_MS`;
- request retries with backoff for retryable errors;
- per-user/backend rate limits;
- provider-specific quota monitoring;
- graceful fallback to cached or mock pricing when limits are reached.

### Enabling eBay Pricing Locally

Keep `PRICING_PROVIDER=mock` for normal development. To test eBay from the
backend only:

```text
PRICING_PROVIDER=ebay
EBAY_ACCESS_TOKEN=your-server-side-oauth-access-token
EBAY_BROWSE_API_URL=https://api.ebay.com/buy/browse/v1/item_summary/search
EBAY_MARKETPLACE_ID=EBAY_AU
EBAY_TIMEOUT_SECONDS=10
PRICING_CACHE_TTL_SECONDS=900
PRICING_PROVIDER_MIN_INTERVAL_MS=250
```

The eBay token must stay in `backend/.env`. Do not pass it to Flutter,
`--dart-define`, app storage, or source code.

### Enabling TCGPlayer Pricing Locally

Keep `PRICING_PROVIDER=mock` for normal development. To test TCGPlayer from the
backend only:

```text
PRICING_PROVIDER=tcgplayer
TCGPLAYER_CLIENT_ID=your-server-side-client-id
TCGPLAYER_CLIENT_SECRET=your-server-side-client-secret
TCGPLAYER_API_BASE=https://api.tcgplayer.com
TCGPLAYER_TIMEOUT_SECONDS=10
PRICING_CACHE_TTL_SECONDS=900
PRICING_PROVIDER_MIN_INTERVAL_MS=250
```

The provider requests an OAuth access token with the client-credentials flow,
searches matching products using name, set, card number, rarity, brand, and
year, then requests product pricing rows and normalizes them into
`PricingResult` and `MarketComparableSale`. Keep credentials in `backend/.env`
only. Do not pass them to Flutter, `--dart-define`, app storage, or source code.

### Enabling PriceCharting Pricing Locally

Keep `PRICING_PROVIDER=mock` for normal development. To test PriceCharting from
the backend only:

```text
PRICING_PROVIDER=pricecharting
PRICECHARTING_API_KEY=your-server-side-pricecharting-key
PRICECHARTING_API_BASE=https://www.pricecharting.com
PRICECHARTING_TIMEOUT_SECONDS=10
PRICING_CACHE_TTL_SECONDS=900
PRICING_PROVIDER_MIN_INTERVAL_MS=250
```

The provider searches PriceCharting products using name, set, card number,
brand, and year, then normalizes guide-price fields such as loose, complete,
new, and graded prices into `PricingResult` and `MarketComparableSale`. Keep the
API key in `backend/.env` only. Do not pass it to Flutter, `--dart-define`, app
storage, or source code.

### Aggregate Pricing Mode

`PRICING_PROVIDER=aggregate` calls the configured eBay, TCGPlayer, and
PriceCharting providers, normalizes comparable sales, removes obvious outliers,
and calculates market value/range/confidence/trend from the blended result. If
every live provider is unavailable, timed out, rate-limited, or empty, the
pipeline falls back to `MockPricingProvider` and returns diagnostics with
`fallbackUsed=true`.

## Real AI + Live Pricing Validation Workflow

Real provider validation is manual/local only. Automated tests mock provider
responses and must not call OpenAI, eBay, TCGPlayer, or PriceCharting.

1. Start mock baseline mode:

```powershell
cd backend
$env:AI_PROVIDER="mock"
$env:PRICING_PROVIDER="mock"
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

2. Validate a local image against the backend contract:

```powershell
py scripts\validate_real_analysis.py C:\path\to\collectible.jpg --category "Pokemon Card"
```

3. Enable real provider mode only when you intend to spend API quota/cost:

```powershell
$env:AI_PROVIDER="openai"
$env:OPENAI_API_KEY="<server-side-openai-key>"
$env:PRICING_PROVIDER="aggregate"
$env:EBAY_ACCESS_TOKEN="your-server-side-ebay-token"
$env:TCGPLAYER_CLIENT_ID="your-server-side-tcgplayer-client-id"
$env:TCGPLAYER_CLIENT_SECRET="your-server-side-tcgplayer-client-secret"
$env:PRICECHARTING_API_KEY="your-server-side-pricecharting-key"
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

4. Run the same validation script and record results in
   `docs/AI_PRICING_VALIDATION.md`.

The validation script prints item name, category, confidence, estimated value,
value range, pricing source, fallback status, latency, image quality warnings,
and alternatives. The `/api/analyze` response includes a backend-only
`diagnostics` object for local validation. Flutter ignores unknown fields and
the core response contract remains unchanged.

Cost warning: OpenAI, eBay, TCGPlayer, and PriceCharting provider calls may
consume paid quota or rate-limited API access. Keep mock mode for normal
development and CI.

## Public Dataset Validation Lab

The validation lab supports user-owned images and public/open dataset exports
without scraping marketplace or search-engine images. It accepts local image
folders, locally downloaded Kaggle folders, locally downloaded Hugging Face
exports, and CSV/JSON ground-truth manifests.

Dataset images are intentionally ignored by git:

```text
validation/images/**
validation/datasets/
validation/downloads/
validation/exports/
validation/raw/
validation/tmp/
```

Only scripts, docs, manifests, and placeholders should be committed.

Safe placeholder run:

```powershell
scripts\run_validation_lab.ps1 -DryRun
```

Prepare a local dataset manifest:

```powershell
scripts\run_validation_lab.ps1 `
  -UserImageFolder C:\path\to\owned-or-open-images `
  -ManifestPath validation\manifests\my_manifest.json `
  -PrepareOnly
```

Import CSV/JSON metadata into the canonical manifest shape:

```powershell
scripts\import_validation_dataset.ps1 `
  -ImageFolder C:\path\to\owned-or-open-images `
  -Metadata C:\path\to\metadata.csv `
  -SourceName "User-owned validation set" `
  -License "user-owned" `
  -OutputManifest validation\manifests\generated_manifest.json
```

Run against the local backend:

```powershell
scripts\run_validation_lab.ps1 `
  -ManifestPath validation\manifests\my_manifest.json `
  -ImageDir validation\images `
  -Endpoint http://127.0.0.1:8000/api/analyze
```

Reports are written to:

```text
validation/reports/latest_validation_report.md
validation/reports/latest_validation_results.csv
```

See `docs/PUBLIC_DATASET_SOURCES.md` for safe source examples and
`docs/VALIDATION_LAB.md` for dataset safety rules, manifest fields, metrics, and
manual real-provider validation guidance.

### Adding Additional Pricing Providers

1. Implement `PricingProvider.price(recognition)`.
2. Read credentials from backend environment only.
3. Normalize provider responses into `PricingResult` and
   `MarketComparableSale`.
4. Raise typed pricing errors for timeout, rate limit, unavailable provider, or
   empty market data.
5. Register the provider in `provider_factory.py`.
6. Add mocked backend tests. Do not call real provider APIs in automated tests.

### Error Responses

Errors use a backend-safe shape:

```json
{
  "success": false,
  "error": {
    "code": "missing_image",
    "message": "Image metadata is required.",
    "retryable": false,
    "details": {}
  }
}
```

Implemented MVP errors:

- `missing_image` with HTTP 400
- `invalid_payload` with HTTP 422
- `unsupported_category` with HTTP 422
- `ai_provider_not_configured` with HTTP 501
- `ai_provider_timeout` with HTTP 504
- `ai_provider_invalid_response` with HTTP 502
- `ai_provider_error` with HTTP 502
- `server_error` with HTTP 500

## Tests

From the `backend` folder:

```powershell
py -m unittest discover tests
```
