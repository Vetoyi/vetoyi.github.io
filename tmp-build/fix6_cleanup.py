from pathlib import Path

p = Path("app/src-tauri/src/health/checker.rs")
s = p.read_text(encoding="utf-8").replace("\r\n", "\n")
old = "fn service_is_running(detail: &str) -> bool {\n"
new = "#[cfg(test)]\nfn service_is_running(detail: &str) -> bool {\n"
if old not in s:
    raise SystemExit("service_is_running helper not found")
s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8", newline="\n")
print("fix6 cleanup applied")
