# Production Analyzer Abstraction

Date: 2026-07-05

## Architecture Diagram

```text
ScannerController
  |
  v
AnalyzerService
  |-- timeout
  |-- retry policy
  |-- cancellation token
  |-- upload/progress events
  |-- normalized AnalyzerException mapping
  |
  v
AnalyzerProvider
  |-- MockAnalyzerProvider
  |     |
  |     v
  |   existing mock/SIT backend recognition path
  |
  |-- FutureVisionProvider placeholder
  |-- FutureOpenAIProvider placeholder
  |-- FutureGeminiProvider placeholder
```

The scanner workflow now depends on `AnalyzerService`, not a concrete AI
provider. Providers are selected by configuration and can be replaced behind
the service without changing scanner UI code.

## Provider Selection

Provider selection uses `AI_ANALYSIS_PROVIDER`.

Supported values:

- `mock`
- `future_vision`
- `future_openai`
- `future_gemini`

Legacy values are still parsed safely:

- `openai`, `openai_vision` -> `future_openai`
- `gemini`, `gemini_vision` -> `future_gemini`

Example provider swap:

```powershell
.\run_sit.bat -d DEVICE_ID
```

uses:

```text
AI_ANALYSIS_PROVIDER=mock
```

Future backend-only OpenAI placeholder:

```powershell
C:\Users\hario\Desktop\flutter\bin\flutter.bat run `
  --flavor sit `
  -d DEVICE_ID `
  --dart-define=APP_ENV=sit `
  --dart-define=AI_ANALYSIS_PROVIDER=future_openai `
  --dart-define=API_BASE_URL=http://YOUR-PC-LAN-IP:8000
```

The placeholder returns a safe provider-unavailable error. Real OpenAI/Gemini
keys must remain server-side and must not be placed in Flutter config.

## Error Mapping

`AnalyzerException` normalizes provider failures into:

- `timeout`
- `network`
- `invalidImage`
- `providerUnavailable`
- `quotaExceeded`
- `authentication`
- `unknown`
- `cancelled`

## Response Model

`AnalyzerResponse` supports the current scanner result and future provider
fields:

- title
- category
- manufacturer
- year
- series
- variant
- condition
- confidence
- estimated value
- currency
- tags
- description
- attributes
- images
- raw provider payload

## Files Created

- `lib/features/ai/domain/analyzer/analyzer_models.dart`
- `lib/features/ai/domain/analyzer/analyzer_provider.dart`
- `lib/features/ai/domain/analyzer/analyzer_service.dart`
- `lib/features/ai/data/analyzer/analyzer_provider_factory.dart`
- `lib/features/ai/data/analyzer/mock_analyzer_provider.dart`
- `lib/features/ai/data/analyzer/future_vision_provider.dart`
- `lib/features/ai/data/analyzer/future_openai_provider.dart`
- `lib/features/ai/data/analyzer/future_gemini_provider.dart`
- `test/analyzer_service_test.dart`

## Files Modified

- `lib/features/ai/services/ai_providers.dart`
- `lib/features/scanner/presentation/controllers/scanner_controller.dart`

## Technical Debt

- The legacy `AiAnalysisProvider` layer remains as a compatibility adapter for
  the current mock/SIT backend path and existing tests.
- Real OpenAI/Gemini integrations still require a trusted backend contract,
  server-side credentials, and provider-specific response validation.
- Cancellation is cooperative at the app service boundary; provider transports
  should wire this into real HTTP cancellation tokens when implemented.
- Upload progress is exposed through analyzer events; real multipart providers
  should emit byte-accurate progress from transport.
