from pathlib import Path

# Temporary CI-only compatibility shim for the layered fix7 -> fix8 -> fix9 build.
# It is imported automatically by Python from the helper-repository root and only
# rewrites the fix9 transform itself. Nothing here is copied into the application.
p = Path("tmp-build/fix9_system_proxy.py")
if p.exists():
    text = p.read_text(encoding="utf-8")
    text = text.replace(
        'features = ["json", "rustls-tls"]',
        'features = ["json", "rustls-tls", "multipart"]',
    )
    text = text.replace(
        'features = ["json", "rustls-tls", "system-proxy"]',
        'features = ["json", "rustls-tls", "multipart", "system-proxy"]',
    )
    p.write_text(text, encoding="utf-8", newline="\n")
