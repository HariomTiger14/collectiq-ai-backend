"""ONE diagnostic request to sportscardspro's CSV endpoint, from wherever it runs.

Exists to answer a single question: is THIS host challenged by Cloudflare on
/price-guide/download-custom? As of 2026-09-01 Render's Ohio IPs are (403 with
cf-mitigated: challenge) while the same token and User-Agent from a residential
connection are not (200, text/csv). Since /api/products is NOT challenged from
the same Ohio host, the rule looks path-scoped and reputation-weighted rather
than a plain IP ban -- which is testable by running this from another region.

Deliberately makes exactly ONE request and never prints the token: the point is
a verdict, not data, and repeatedly probing a host that is already refusing us
is the surest way to turn a temporary block into a durable one.

Prints the verdict only -- never the token. Run in a Render shell:
    python render_probe.py
"""
import os, time, httpx

TOKEN = os.getenv("PRICECHARTING_API_TOKEN") or os.getenv("PRICECHARTING_API_KEY") or ""
if not TOKEN:
    raise SystemExit("PRICECHARTING_API_TOKEN not set in this service's env.")

UA = {"User-Agent": "PackLoxSetDiscoveryBot/1.0 (+https://packlox.com; Legendary subscriber)"}
URL = "https://www.sportscardspro.com/price-guide/download-custom"

print(f"WHERE   : {os.getenv('RENDER_SERVICE_NAME', 'unknown service')}")
t0 = time.perf_counter()
try:
    with httpx.Client(timeout=60, follow_redirects=True, headers=UA) as h:
        r = h.get(URL, params={"t": TOKEN, "console-uids": "G47162"})
except Exception as exc:
    raise SystemExit(f"TRANSPORT ERROR: {type(exc).__name__}: {exc}")

print(f"STATUS  : {r.status_code}")
print(f"ELAPSED : {time.perf_counter() - t0:.2f}s")
for k in ("server", "content-type", "cf-ray", "cf-mitigated", "retry-after"):
    if k in r.headers:
        print(f"HDR {k:<14}: {r.headers[k]}")

body = r.text[:200].replace("\n", " ")
print(f"BODY    : {body!r}")

ctype = r.headers.get("content-type", "")
challenged = "cf-mitigated" in r.headers or "Just a moment" in r.text
if r.status_code == 200 and "csv" in ctype:
    print("VERDICT : OK -- this region is NOT blocked (real CSV returned)")
elif challenged:
    print("VERDICT : BLOCKED BY CLOUDFLARE (edge challenge, request never reached the app)")
elif r.status_code == 429:
    print("VERDICT : RATE LIMITED (throttle, not a block) -- slow down, do not migrate")
else:
    print(f"VERDICT : UNCLEAR -- status {r.status_code}, content-type {ctype!r}")

# cf-ray's suffix is the Cloudflare edge PoP, which tells you which region
# the request actually egressed toward: CMH=Columbus/Ohio, PDX=Oregon,
# IAD=Virginia, FRA=Frankfurt, SIN=Singapore, MEL=Melbourne.
print("NOTE    : compare the cf-ray suffix against the old Ohio run (…-CMH)")
