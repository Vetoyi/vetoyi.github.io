from pathlib import Path


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 1) reqwest was built with default-features=false, so the existing "system"
# proxy branches never had Windows/macOS system proxy support available.
# Enable only reqwest's system-proxy feature while retaining rustls.
# ---------------------------------------------------------------------------
p = Path("app/src-tauri/Cargo.toml")
s = read(p)
old = 'reqwest = { version = "0.12", default-features = false, features = ["json", "rustls-tls"] }'
new = 'reqwest = { version = "0.12", default-features = false, features = ["json", "rustls-tls", "system-proxy"] }'
if old not in s:
    raise SystemExit("reqwest dependency feature line not found")
s = s.replace(old, new, 1)
write(p, s)


# ---------------------------------------------------------------------------
# 2) Health checker proxy semantics.
# ---------------------------------------------------------------------------
p = Path("app/src-tauri/src/health/checker.rs")
s = read(p)

start = s.find("fn resolve_proxy_url(proxy: &ProxyConfig) -> Option<String> {")
end = s.find("\nfn request_error_detail", start)
if start == -1 or end == -1:
    raise SystemExit("health proxy helper region not found")
new_region = r'''#[derive(Debug, Clone, PartialEq, Eq)]
enum OutboundProxyPolicy {
    Direct,
    System,
    Manual(String),
}

fn outbound_proxy_policy(use_proxy: bool, proxy: &ProxyConfig) -> OutboundProxyPolicy {
    if !use_proxy {
        return OutboundProxyPolicy::Direct;
    }
    match proxy.mode.trim() {
        "system" => OutboundProxyPolicy::System,
        "manual" if !proxy.url.trim().is_empty() => {
            OutboundProxyPolicy::Manual(proxy.url.trim().to_string())
        }
        _ => OutboundProxyPolicy::Direct,
    }
}

fn http_client(use_proxy: bool) -> reqwest::Client {
    let settings = AppSettings::load_or_default();
    let mut builder = reqwest::Client::builder().timeout(TIMEOUT);
    match outbound_proxy_policy(use_proxy, &settings.proxy) {
        OutboundProxyPolicy::Direct => {
            builder = builder.no_proxy();
        }
        OutboundProxyPolicy::System => {
            // reqwest is compiled with `system-proxy`; on Windows it reads
            // the current user's Internet Settings.
        }
        OutboundProxyPolicy::Manual(url) => {
            // Manual mode is authoritative: clear automatic/system proxies first.
            builder = builder.no_proxy();
            if let Ok(proxy) = reqwest::Proxy::all(&url) {
                builder = builder.proxy(proxy);
            }
        }
    }
    builder.build().expect("failed to build HTTP client")
}
'''
s = s[:start] + new_region + s[end:]

s += r'''

#[cfg(test)]
mod fix9_system_proxy_tests {
    use super::{outbound_proxy_policy, OutboundProxyPolicy};
    use crate::settings::ProxyConfig;

    #[test]
    fn disabled_service_proxy_is_always_direct() {
        let proxy = ProxyConfig {
            mode: "system".into(),
            url: String::new(),
        };
        assert_eq!(
            outbound_proxy_policy(false, &proxy),
            OutboundProxyPolicy::Direct
        );
    }

    #[test]
    fn system_mode_uses_real_system_proxy_policy() {
        let proxy = ProxyConfig {
            mode: "system".into(),
            url: String::new(),
        };
        assert_eq!(
            outbound_proxy_policy(true, &proxy),
            OutboundProxyPolicy::System
        );
    }

    #[test]
    fn manual_mode_is_explicit_and_none_is_direct() {
        let manual = ProxyConfig {
            mode: "manual".into(),
            url: "http://127.0.0.1:7890".into(),
        };
        assert_eq!(
            outbound_proxy_policy(true, &manual),
            OutboundProxyPolicy::Manual("http://127.0.0.1:7890".into())
        );

        let none = ProxyConfig {
            mode: "none".into(),
            url: "http://127.0.0.1:7890".into(),
        };
        assert_eq!(
            outbound_proxy_policy(true, &none),
            OutboundProxyPolicy::Direct
        );
    }
}
'''
write(p, s)


# ---------------------------------------------------------------------------
# 3) Relay clients use exactly the same semantics.
# ---------------------------------------------------------------------------
p = Path("app/src-tauri/src/relay/mod.rs")
s = read(p)
old = r'''fn resolve_proxy_url(proxy: &ProxyConfig) -> Option<String> {
    match proxy.mode.as_str() {
        "manual" if !proxy.url.trim().is_empty() => Some(proxy.url.trim().to_string()),
        "system" => std::env::var("HTTPS_PROXY")
            .ok()
            .filter(|value| !value.trim().is_empty())
            .or_else(|| {
                std::env::var("HTTP_PROXY")
                    .ok()
                    .filter(|value| !value.trim().is_empty())
            })
            .or_else(|| {
                std::env::var("ALL_PROXY")
                    .ok()
                    .filter(|value| !value.trim().is_empty())
            }),
        _ => None,
    }
}

fn apply_configured_proxy(
    mut builder: reqwest::ClientBuilder,
    use_proxy: bool,
) -> reqwest::ClientBuilder {
    if use_proxy {
        let settings = AppSettings::load_or_default();
        if let Some(url) = resolve_proxy_url(&settings.proxy) {
            if let Ok(proxy) = reqwest::Proxy::all(&url) {
                builder = builder.proxy(proxy);
            }
        }
    }
    builder
}
'''
new = r'''#[derive(Debug, Clone, PartialEq, Eq)]
enum RelayProxyPolicy {
    Direct,
    System,
    Manual(String),
}

fn relay_proxy_policy(use_proxy: bool, proxy: &ProxyConfig) -> RelayProxyPolicy {
    if !use_proxy {
        return RelayProxyPolicy::Direct;
    }
    match proxy.mode.trim() {
        "system" => RelayProxyPolicy::System,
        "manual" if !proxy.url.trim().is_empty() => {
            RelayProxyPolicy::Manual(proxy.url.trim().to_string())
        }
        _ => RelayProxyPolicy::Direct,
    }
}

fn apply_configured_proxy(
    mut builder: reqwest::ClientBuilder,
    use_proxy: bool,
) -> AppResult<reqwest::ClientBuilder> {
    let settings = AppSettings::load_or_default();
    match relay_proxy_policy(use_proxy, &settings.proxy) {
        RelayProxyPolicy::Direct => {
            builder = builder.no_proxy();
        }
        RelayProxyPolicy::System => {
            // Leave reqwest's system-proxy matcher enabled.
        }
        RelayProxyPolicy::Manual(url) => {
            let proxy = reqwest::Proxy::all(&url)
                .map_err(|error| AppError::Message(format!("全局手动代理地址无效: {error}")))?;
            builder = builder.no_proxy().proxy(proxy);
        }
    }
    Ok(builder)
}
'''
if old not in s:
    raise SystemExit("fix8 relay proxy helpers not found")
s = s.replace(old, new, 1)

old = r'''    apply_configured_proxy(
        reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(8))
            .timeout(Duration::from_secs(15)),
        use_proxy,
    )
    .build()
'''
new = r'''    apply_configured_proxy(
        reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(8))
            .timeout(Duration::from_secs(15)),
        use_proxy,
    )?
    .build()
'''
if old not in s:
    raise SystemExit("relay_client apply block not found")
s = s.replace(old, new, 1)

old = r'''    apply_configured_proxy(
        reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(40))
            .user_agent("Coding-Tools-MCP-Relay/0.2.0"),
        use_proxy,
    )
    .build()
'''
new = r'''    apply_configured_proxy(
        reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(40))
            .user_agent("Coding-Tools-MCP-Relay/0.2.0"),
        use_proxy,
    )?
    .build()
'''
if old not in s:
    raise SystemExit("cloudflare_client apply block not found")
s = s.replace(old, new, 1)

old = r'''    #[test]
    fn relay_manual_proxy_resolution_matches_health_checker_policy() {
        let proxy = ProxyConfig {
            mode: "manual".into(),
            url: "http://127.0.0.1:7890".into(),
        };
        assert_eq!(resolve_proxy_url(&proxy).as_deref(), Some("http://127.0.0.1:7890"));

        let disabled = ProxyConfig {
            mode: "none".into(),
            url: "http://127.0.0.1:7890".into(),
        };
        assert_eq!(resolve_proxy_url(&disabled), None);
    }
'''
new = r'''    #[test]
    fn relay_proxy_policy_distinguishes_system_manual_and_direct() {
        let system = ProxyConfig {
            mode: "system".into(),
            url: String::new(),
        };
        assert_eq!(
            relay_proxy_policy(true, &system),
            RelayProxyPolicy::System
        );

        let manual = ProxyConfig {
            mode: "manual".into(),
            url: "http://127.0.0.1:7890".into(),
        };
        assert_eq!(
            relay_proxy_policy(true, &manual),
            RelayProxyPolicy::Manual("http://127.0.0.1:7890".into())
        );

        assert_eq!(
            relay_proxy_policy(false, &system),
            RelayProxyPolicy::Direct
        );
    }
'''
if old not in s:
    raise SystemExit("fix8 relay proxy unit test not found")
s = s.replace(old, new, 1)
write(p, s)


# ---------------------------------------------------------------------------
# 4) Binary download client already intended "system" to mean reqwest system
# proxy. Make manual mode use proxy_url and override automatic proxies.
# ---------------------------------------------------------------------------
p = Path("app/src-tauri/src/tunnel/download.rs")
s = read(p)
old = r'''    match mode {
        "" | "none" => {
            builder = builder.no_proxy();
        }
        "system" => {
            // Leave reqwest's default system-proxy detection enabled.
        }
        url => {
            let proxy = reqwest::Proxy::all(url)
                .map_err(|err| AppError::Message(format!("代理地址无效: {err}")))?;
            builder = builder.proxy(proxy);
        }
    }
'''
new = r'''    match mode {
        "" | "none" => {
            builder = builder.no_proxy();
        }
        "system" => {
            // reqwest `system-proxy`: Windows Internet Settings / macOS system proxy.
        }
        "manual" => {
            let url = settings.download.proxy_url.trim();
            if url.is_empty() {
                return Err(AppError::Message(
                    "下载代理模式为手动，但未填写代理地址。".into(),
                ));
            }
            let proxy = reqwest::Proxy::all(url)
                .map_err(|err| AppError::Message(format!("代理地址无效: {err}")))?;
            builder = builder.no_proxy().proxy(proxy);
        }
        url => {
            // Backward compatibility with legacy configs that stored the URL in proxy_mode.
            let proxy = reqwest::Proxy::all(url)
                .map_err(|err| AppError::Message(format!("代理地址无效: {err}")))?;
            builder = builder.no_proxy().proxy(proxy);
        }
    }
'''
if old not in s:
    raise SystemExit("download proxy match block not found")
s = s.replace(old, new, 1)
write(p, s)


# ---------------------------------------------------------------------------
# 5) Update checker: manual/legacy explicit proxy overrides system proxy.
# ---------------------------------------------------------------------------
p = Path("app/src-tauri/src/update/mod.rs")
s = read(p)
old_count = s.count("            builder = builder.proxy(proxy);\n")
if old_count < 2:
    raise SystemExit(f"expected at least 2 update proxy assignments, got {old_count}")
s = s.replace(
    "            builder = builder.proxy(proxy);\n",
    "            builder = builder.no_proxy().proxy(proxy);\n",
)
write(p, s)

print("fix9 real system proxy semantics applied")
