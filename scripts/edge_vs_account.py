"""Is the block at Cloudflare's EDGE, or is it specific to our ACCOUNT?

Two explanations fit the evidence so far and they call for different actions:

  A. Edge/bot rule on the path. Cloudflare rules commonly exclude /api/* while
     challenging everything else, so datacenter IPs fail on /price-guide/* and
     pass on /api/*. Nothing to do with our account or our request rate.
  B. Our account was flagged, plausibly for exceeding the published
     one-CSV-call-per-10-minutes limit.

Distinguishing them: send the SAME request with NO token.

  * challenged again  -> the edge blocks before anything reads the token, so
    the rule cannot be account-specific. Explanation A.
  * an application response instead (401, or JSON saying the token is
    missing/invalid) -> the edge let us through and the challenge we normally
    get is about WHO we are. Explanation B.

Three requests, no credentials used anywhere. Run in a Render shell.
"""

import time

import httpx

UA = {"User-Agent": "PackLoxSetDiscoveryBot/1.0 (+https://packlox.com; Legendary subscriber)"}
BASE = "https://www.sportscardspro.com"


def probe(label: str, path: str, params: dict | None = None) -> str:
    try:
        with httpx.Client(timeout=60, follow_redirects=True, headers=UA) as h:
            r = h.get(f"{BASE}{path}", params=params or {})
    except Exception as exc:
        print(f"  {label:<34} TRANSPORT ERROR {type(exc).__name__}")
        return "error"

    ctype = r.headers.get("content-type", "")[:24]
    ray = r.headers.get("cf-ray", "-")
    mitigated = r.headers.get("cf-mitigated")
    challenged = bool(mitigated) or "Just a moment" in r.text

    if challenged:
        outcome = "CHALLENGED by Cloudflare"
    elif r.status_code == 200 and "csv" in ctype:
        outcome = "200 CSV (not blocked at all)"
    else:
        outcome = f"{r.status_code} {ctype} <- reached their app"
    print(f"  {label:<34} {outcome}")
    print(f"  {'':<34} cf-ray {ray}  body {r.text[:70]!r}")
    return "challenged" if challenged else "reached-app"


print("Sending three requests, none of them authenticated.\n")

a = probe("download-custom, NO token", "/price-guide/download-custom",
          {"console-uids": "G47162"})
time.sleep(2)
b = probe("api/products, NO token", "/api/products", {"q": "michael jordan"})
time.sleep(2)
c = probe("a plain set page, NO token", "/console/baseball-cards-2021-panini-mosaic")

print()
if a == "challenged" and c == "challenged":
    print("VERDICT: EDGE RULE on this host from this IP -- it challenges before")
    print("         reading any token, so it is NOT about our account, and our")
    print("         rate-limit breach is not the cause.")
elif a == "challenged" and c == "reached-app":
    print("VERDICT: the challenge is scoped to /price-guide/*, not the whole site.")
    print("         Still an edge/path rule rather than an account flag, since no")
    print("         token was sent.")
elif a == "reached-app":
    print("VERDICT: the edge let an UNAUTHENTICATED request through, so the block")
    print("         we normally hit depends on WHO we are -- account-specific.")
else:
    print("VERDICT: mixed/unclear -- paste the output.")
