import urllib.request

# CI-only deterministic wrapper. The original fix9 transform is pinned to the
# immutable commit where it was first added; fix7 had already added reqwest's
# `multipart` feature, so adapt only that dependency line before executing it.
SOURCE_URL = (
    "https://raw.githubusercontent.com/Vetoyi/vetoyi.github.io/"
    "222c69a06d012698c541ec067104d153b1492f70/tmp-build/fix9_system_proxy.py"
)

with urllib.request.urlopen(SOURCE_URL, timeout=30) as response:
    source = response.read().decode("utf-8")

old_before = 'reqwest = { version = "0.12", default-features = false, features = ["json", "rustls-tls"] }'
old_after = 'reqwest = { version = "0.12", default-features = false, features = ["json", "rustls-tls", "multipart"] }'
new_before = 'reqwest = { version = "0.12", default-features = false, features = ["json", "rustls-tls", "system-proxy"] }'
new_after = 'reqwest = { version = "0.12", default-features = false, features = ["json", "rustls-tls", "multipart", "system-proxy"] }'

if old_before not in source or new_before not in source:
    raise SystemExit("pinned fix9 transform dependency markers changed unexpectedly")
source = source.replace(old_before, old_after, 1).replace(new_before, new_after, 1)

exec(compile(source, SOURCE_URL, "exec"), {"__name__": "__main__"})
