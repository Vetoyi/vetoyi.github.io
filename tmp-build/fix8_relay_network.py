from pathlib import Path


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# Relay networking must obey the same per-service "use proxy" switch as the
# Quick Tunnel and public health checker.  Also distinguish transport failures
# from real Worker/protocol failures so a successfully uploaded Worker is not
# reported as "deployment failed" just because this machine cannot hairpin to
# workers.dev immediately.
# ---------------------------------------------------------------------------
p = Path("app/src-tauri/src/relay/mod.rs")
s = read(p)

old = '''use crate::error::{AppError, AppResult};
use crate::tunnel::TunnelServiceKind;
use crate::workspace::{RelayConfig, WorkspaceProfile};
'''
new = '''use crate::error::{AppError, AppResult};
use crate::settings::{AppSettings, ProxyConfig};
use crate::tunnel::TunnelServiceKind;
use crate::workspace::{RelayConfig, WorkspaceProfile};
'''
if old not in s:
    raise SystemExit("relay imports pattern not found")
s = s.replace(old, new, 1)

marker = '''struct PublicRelayHealth {
    #[serde(default)]
    ok: bool,
    #[serde(default)]
    relay_version: String,
    #[serde(default)]
    service: String,
    #[serde(default)]
    backend: String,
    #[serde(default)]
    expires_in: u64,
}
'''
insert = marker + '''
#[derive(Debug)]
enum RelayProbeError {
    Transport(String),
    Protocol(String),
}

fn relay_uses_proxy(profile: &WorkspaceProfile, kind: TunnelServiceKind) -> bool {
    match kind {
        TunnelServiceKind::Mcp => profile.tunnel.use_proxy,
        TunnelServiceKind::Actions => profile.actions.use_proxy,
    }
}

fn resolve_proxy_url(proxy: &ProxyConfig) -> Option<String> {
    match proxy.mode.as_str() {
        "manual" if !proxy.url.trim().is_empty() => Some(proxy.url.trim().to_string()),
        "system" => std::env::var("HTTPS_PROXY")
            .ok()
            .filter(|value| !value.trim().is_empty())
            .or_else(|| std::env::var("HTTP_PROXY").ok().filter(|value| !value.trim().is_empty()))
            .or_else(|| std::env::var("ALL_PROXY").ok().filter(|value| !value.trim().is_empty())),
        _ => None,
    }
}

fn configure_proxy(
    builder: reqwest::ClientBuilder,
    profile: &WorkspaceProfile,
    kind: TunnelServiceKind,
) -> AppResult<reqwest::ClientBuilder> {
    if !relay_uses_proxy(profile, kind) {
        return Ok(builder.no_proxy());
    }

    let settings = AppSettings::load_or_default();
    let Some(url) = resolve_proxy_url(&settings.proxy) else {
        // "use proxy" with mode=none/system-without-env has no effective proxy;
        // keep semantics identical to cloudflared's apply_proxy_env().
        return Ok(builder.no_proxy());
    };
    let proxy = reqwest::Proxy::all(&url).map_err(|_| {
        AppError::Message(
            "网络代理配置无效，请检查「设置 → 通用 → 网络代理」；Relay 未使用该代理发送请求。"
                .into(),
        )
    })?;
    Ok(builder.proxy(proxy))
}
'''
if marker not in s:
    raise SystemExit("PublicRelayHealth marker not found")
s = s.replace(marker, insert, 1)

# Management API and Worker traffic both follow the service's proxy switch.
s = s.replace('let client = cloudflare_client()?;', 'let client = cloudflare_client(profile, kind)?;')
s = s.replace('let client = relay_client()?;', 'let client = relay_client(profile, kind)?;')

old = '''    let public_url = format!("https://{worker_name}.{account_subdomain}.workers.dev");
    let health = probe_url(&public_url).await?;
    if !health.ok || health.service != service_name(kind) || health.relay_version != "1" {
        return Err(AppError::Message(format!(
            "Relay 已部署但自检返回异常：service={}, version={}, backend={}",
            health.service, health.relay_version, health.backend
        )));
    }

    Ok(RelayDeploymentResult {
        public_url,
        worker_name,
        account_id: account_id.trim().to_string(),
        deployment_version: RELAY_DEPLOYMENT_VERSION,
        message: "Cloudflare Quick Tunnel Relay 已部署；Cloudflare API Token 未保存。".into(),
    })
'''
new = '''    let public_url = format!("https://{worker_name}.{account_subdomain}.workers.dev");
    let mut transport_warning = None;
    match probe_url(profile, kind, &public_url).await {
        Ok(health) => {
            if !health.ok || health.service != service_name(kind) || health.relay_version != "1" {
                return Err(AppError::Message(format!(
                    "Relay Worker 已上传，但自检返回了不匹配的服务身份：service={}, version={}, backend={}。为避免把错误 Worker 保存成稳定入口，本地配置未启用。",
                    health.service, health.relay_version, health.backend
                )));
            }
        }
        Err(RelayProbeError::Protocol(detail)) => {
            return Err(AppError::Message(format!(
                "Relay Worker 已上传，但 workers.dev 返回了实际 HTTP/协议异常：{detail}。为避免保存错误入口，本地配置未启用。"
            )));
        }
        Err(RelayProbeError::Transport(detail)) => {
            // Upload + workers.dev enablement have already been confirmed by the
            // Cloudflare management API. A local DNS/proxy/hairpin failure is not
            // evidence that the Worker deployment itself failed. Save the stable URL
            // and let signed target renewal retry on the same endpoint.
            transport_warning = Some(detail);
        }
    }

    let message = if let Some(detail) = transport_warning {
        format!(
            "Cloudflare Quick Tunnel Relay 已部署并保存稳定地址；但本机暂时无法完成 workers.dev 自检：{detail}。这属于本机到 Relay 的网络 transport 问题，不会重新创建 Worker；程序会继续在同一地址自动重试 target 同步/续租。若该服务勾选“使用网络代理”，Relay 请求会使用同一全局代理。Cloudflare API Token 未保存。"
        )
    } else {
        "Cloudflare Quick Tunnel Relay 已部署并通过自检；Cloudflare API Token 未保存。".into()
    };

    Ok(RelayDeploymentResult {
        public_url,
        worker_name,
        account_id: account_id.trim().to_string(),
        deployment_version: RELAY_DEPLOYMENT_VERSION,
        message,
    })
'''
if old not in s:
    raise SystemExit("deployment health block not found")
s = s.replace(old, new, 1)

s = s.replace(
    'match probe_url(&public_url).await {',
    'match probe_url(profile, kind, &public_url).await {',
    1,
)

old = '''        Err(error) => RelayRemoteStatus {
            configured: true,
            public_url,
            state: "unreachable".into(),
            backend: "unknown".into(),
            expires_in: 0,
            detail: format!("Relay 自检失败: {error}"),
        },
'''
new = '''        Err(RelayProbeError::Transport(detail)) => RelayRemoteStatus {
            configured: true,
            public_url,
            state: "unreachable".into(),
            backend: "unknown".into(),
            expires_in: 0,
            detail: format!(
                "本机无法访问 Relay（transport）：{detail}。若外部 ChatGPT 可访问该稳定地址，这通常是本机代理/DNS/回环路径问题；自动续租会继续重试。"
            ),
        },
        Err(RelayProbeError::Protocol(detail)) => RelayRemoteStatus {
            configured: true,
            public_url,
            state: "invalid".into(),
            backend: "unknown".into(),
            expires_in: 0,
            detail: format!("Relay 返回实际 HTTP/协议异常：{detail}"),
        },
'''
if old not in s:
    raise SystemExit("remote status error arm not found")
s = s.replace(old, new, 1)

old = '''async fn probe_url(public_url: &str) -> AppResult<PublicRelayHealth> {
    let url = format!("{}/__ctm/health", public_url.trim_end_matches('/'));
    let mut last_error = String::new();
    for (attempt, delay_ms) in [0u64, 500, 1200, 2500].into_iter().enumerate() {
        if delay_ms > 0 {
            tokio::time::sleep(Duration::from_millis(delay_ms)).await;
        }
        match relay_client()?.get(&url).send().await {
            Ok(response) => {
                let status = response.status();
                if !status.is_success() {
                    last_error = format!("Relay 健康端点返回 HTTP {status}");
                    continue;
                }
                match response.json::<PublicRelayHealth>().await {
                    Ok(health) => return Ok(health),
                    Err(error) => {
                        last_error = format!("Relay 健康响应格式错误: {error}");
                    }
                }
            }
            Err(error) => {
                last_error = format!("访问 Relay 健康端点失败: {error}");
            }
        }
        if attempt == 3 {
            break;
        }
    }
    Err(AppError::Message(last_error))
}

fn relay_client() -> AppResult<reqwest::Client> {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(8))
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|e| AppError::Message(format!("创建 Relay HTTP 客户端失败: {e}")))
}

fn cloudflare_client() -> AppResult<reqwest::Client> {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(40))
        .user_agent("Coding-Tools-MCP-Relay/0.2.0")
        .build()
        .map_err(|e| AppError::Message(format!("创建 Cloudflare API 客户端失败: {e}")))
}
'''
new = '''async fn probe_url(
    profile: &WorkspaceProfile,
    kind: TunnelServiceKind,
    public_url: &str,
) -> Result<PublicRelayHealth, RelayProbeError> {
    let url = format!("{}/__ctm/health", public_url.trim_end_matches('/'));
    let client = relay_client(profile, kind)
        .map_err(|error| RelayProbeError::Protocol(error.to_string()))?;
    let mut last_transport = None;
    let mut last_protocol = None;

    for (attempt, delay_ms) in [0u64, 500, 1200, 2500].into_iter().enumerate() {
        if delay_ms > 0 {
            tokio::time::sleep(Duration::from_millis(delay_ms)).await;
        }
        match client.get(&url).send().await {
            Ok(response) => {
                let status = response.status();
                if !status.is_success() {
                    last_protocol = Some(format!("Relay 健康端点返回 HTTP {status}"));
                    continue;
                }
                match response.json::<PublicRelayHealth>().await {
                    Ok(health) => return Ok(health),
                    Err(error) => {
                        last_protocol = Some(format!("Relay 健康响应格式错误: {error}"));
                    }
                }
            }
            Err(error) => {
                last_transport = Some(if error.is_timeout() {
                    format!("请求超时: {error}")
                } else if error.is_connect() {
                    format!("连接失败: {error}")
                } else {
                    format!("请求失败: {error}")
                });
            }
        }
        if attempt == 3 {
            break;
        }
    }

    if let Some(detail) = last_protocol {
        Err(RelayProbeError::Protocol(detail))
    } else {
        Err(RelayProbeError::Transport(
            last_transport.unwrap_or_else(|| "workers.dev 自检未得到响应".into()),
        ))
    }
}

fn relay_client(profile: &WorkspaceProfile, kind: TunnelServiceKind) -> AppResult<reqwest::Client> {
    let builder = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(8))
        .timeout(Duration::from_secs(15));
    configure_proxy(builder, profile, kind)?
        .build()
        .map_err(|e| AppError::Message(format!("创建 Relay HTTP 客户端失败: {e}")))
}

fn cloudflare_client(profile: &WorkspaceProfile, kind: TunnelServiceKind) -> AppResult<reqwest::Client> {
    let builder = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(40))
        .user_agent("Coding-Tools-MCP-Relay/0.2.0");
    configure_proxy(builder, profile, kind)?
        .build()
        .map_err(|e| AppError::Message(format!("创建 Cloudflare API 客户端失败: {e}")))
}
'''
if old not in s:
    raise SystemExit("hardened probe/client block not found")
s = s.replace(old, new, 1)

# Add deterministic tests that do not need a live proxy or Cloudflare account.
insert = '''

    #[test]
    fn relay_proxy_switch_follows_service_configuration() {
        let mut profile = WorkspaceProfile::new("C:/repo".into(), Some("repo".into()));
        profile.tunnel.use_proxy = true;
        profile.actions.use_proxy = false;
        assert!(relay_uses_proxy(&profile, TunnelServiceKind::Mcp));
        assert!(!relay_uses_proxy(&profile, TunnelServiceKind::Actions));
    }

    #[test]
    fn manual_proxy_url_is_resolved_without_logging_credentials() {
        let proxy = ProxyConfig {
            mode: "manual".into(),
            url: "http://127.0.0.1:7890".into(),
        };
        assert_eq!(resolve_proxy_url(&proxy).as_deref(), Some("http://127.0.0.1:7890"));
    }
'''
pos = s.rfind('\n}')
if pos == -1:
    raise SystemExit("relay test module closing brace not found")
s = s[:pos] + insert + s[pos:]
write(p, s)

# ---------------------------------------------------------------------------
# If the stable identity change requires one local service restart and that
# restart fails, the Worker/config already exist. Report success-with-warning
# instead of returning an error that leaves the UI claiming "not deployed".
# ---------------------------------------------------------------------------
p = Path("app/src-tauri/src/commands/relay.rs")
s = read(p)
old = '''    if previous_canonical != next_canonical {
        if let Err(error) = restart_if_running(&state, &id, kind).await {
            return Err(AppError::Message(format!(
                "Relay 已部署到 {}，但当前服务自动重启失败：{error}。Relay 配置已保存；请手动重新启动该服务一次。",
                result.public_url
            )));
        }
    }
'''
new = '''    if previous_canonical != next_canonical {
        if let Err(error) = restart_if_running(&state, &id, kind).await {
            result.message.push_str(&format!(
                " Relay 配置已经保存，但当前服务自动重启失败：{error}。请手动重新启动该服务一次；不要重新部署 Worker。"
            ));
        }
    }
'''
if old not in s:
    raise SystemExit("relay restart warning block not found")
s = s.replace(old, new, 1)
write(p, s)

print("fix8 relay network/proxy/deployment semantics applied")
