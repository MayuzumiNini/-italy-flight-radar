import json, os, urllib.request, urllib.error
key = os.environ.get("APISH_API_KEY", "")
if not key:
    raise SystemExit("APISH_API_KEY missing")
req = urllib.request.Request("https://api.api.sh/v1/account", headers={"x-api-key": key})
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.load(r)
except urllib.error.HTTPError as e:
    raise SystemExit("Account check HTTP " + str(e.code))
def clean(value, path=""):
    if isinstance(value, dict):
        return {k: clean(v, path + "." + k) for k,v in value.items() if not any(s in k.lower() for s in ("email","token","secret","name","id","key","card","url"))}
    if isinstance(value, list):
        return [clean(v, path) for v in value[:5]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if any(s in path.lower() for s in ("reset","renew","plan","currency","period","billing","auto")):
        return str(value).replace(key, "[REDACTED]")[:100]
    return "[string]"
print(json.dumps(clean(body), ensure_ascii=False, indent=2))
