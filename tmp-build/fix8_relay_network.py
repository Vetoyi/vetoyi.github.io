from pathlib import Path


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")

# ---------------------------------------------------------------------------
# Relay outbound networking must honor the same per-service "use network proxy"
# switch + global proxy settings as Quick Tunnel and public health checks.
# ---------------------------------------------------------------------------
p = Path("app/src-tauri/src/relay/mod.rs")
s = read(p)

old = '''use crate::error::{AppError, AppResult};
use crate::tunnel::TunnelServiceKind;
'''
new = '''use crate::error::{AppError, AppResult};
use crate::settings::{AppSettings, ProxyConfig};
use crate::tunnel::TunnelServiceKind;
'''
if old not in s:
    raise SystemExit("relay settings import point not found")
s = s.replace(old, new, 1)

marker = '''pub fn service_name(kind: TunnelServiceKind) -> &'static str {
    match kind {
        TunnelServiceKind::Mcp => "mcp",
        TunnelServiceKind::Actions => "actions",
    }
}
'''
insert = marker + '''
fn service_uses_proxy(profile: &WorkspaceProfile, kind: TunnelServiceKind) -> bool {
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
if marker not in s:
    raise SystemExit("service_name block not found")
s = s.replace(marker, insert, 1)

# Cloudflare deployment/delete API should follow the same networking policy too.
old = '''    let identity = load_or_create_identity(&profile.id, kind)?;
    let public_key_b64 = URL_SAFE_NO_PAD.encode(identity.signing_key.verifying_key().as_bytes());
    let client = cloudflare_client()?;
'''
new = '''    let identity = load_or_create_identity(&profile.id, kind)?;
    let public_key_b64 = URL_SAFE_NO_PAD.encode(identity.signing_key.verifying_key().as_bytes());
    let use_proxy = service_uses_proxy(profile, kind);
    let client = cloudflare_client(use_proxy)?;
'''
if old not in s:
    raise SystemExit("deploy cloudflare client block not found")
s = s.replace(old, new, 1)

# Deployment is a two-stage operation: Cloudflare API mutation, then verification.
# Once upload + workers.dev enable succeeded, a LOCAL transport failure must not be
# reported as if the remote deployment never happened. Protocol/identity mismatch
# remains a hard failure.
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
    let mut message = "Cloudflare Quick Tunnel Relay 已部署；Cloudflare API Token 未保存。".to_string();
    match probe_url(&public_url, use_proxy).await {
        Ok(health) => {
            if !health.ok || health.service != service_name(kind) || health.relay_version != "1" {
                return Err(AppError::Message(format!(
                    "Relay 已部署但自检返回的服务身份异常：service={}, version={}, backend={}。为安全起见未启用本地 Relay 配置。",
                    health.service, health.relay_version, health.backend
                )));
            }
        }
        Err(error) if relay_probe_is_transport_failure(&error) => {
            message.push_str(&format!(
                " 本机暂时无法完成 workers.dev 健康自检：{error}。远端 Worker 已部署，本地配置将保留；可稍后刷新状态，无需重复创建 Worker。"
            ));
        }
        Err(error) => return Err(error),
    }

    Ok(RelayDeploymentResult {
        public_url,
        worker_name,
        account_id: account_id.trim().to_string(),
        deployment_version: RELAY_DEPLOYMENT_VERSION,
        message,
    })
'''
if old not in s:
    raise SystemExit("deploy probe block not found")
s = s.replace(old, new, 1)

old = '''    let client = cloudflare_client()?;
    let response = client
'''
new = '''    let client = cloudflare_client(service_uses_proxy(profile, kind))?;
    let response = client
'''
if old not in s:
    raise SystemExit("delete cloudflare client block not found")
s = s.replace(old, new, 1)

old = '''    match probe_url(&public_url).await {
'''
new = '''    match probe_url(&public_url, service_uses_proxy(profile, kind)).await {
'''
if old not in s:
    raise SystemExit("remote status probe call not found")
s = s.replace(old, new, 1)

old = '''    let client = relay_client()?;
    let response = client
'''
new = '''    let client = relay_client(service_uses_proxy(profile, kind))?;
    let response = client
'''
if old not in s:
    raise SystemExit("send_update relay client block not found")
s = s.replace(old, new, 1)

# Replace probe/client helpers. Keep protocol failures distinct from pure transport
# failures so only the latter can be downgraded after a confirmed Cloudflare deploy.
start = s.find('async fn probe_url(public_url: &str) -> AppResult<PublicRelayHealth> {')
end = s.find('\nasync fn cf_json<', start)
if start == -1 or end == -1:
    raise SystemExit("probe/client helper region not found")
old_region = s[start:end]
new_region = '''async fn probe_url(public_url: &str, use_proxy: bool) -> AppResult<PublicRelayHealth> {
    let url = format!("{}/__ctm/health", public_url.trim_end_matches('/'));
    let mut last_transport_error: Option<String> = None;
    let mut last_protocol_error: Option<String> = None;
    for delay_ms in [0u64, 500, 1200, 2500] {
        if delay_ms > 0 {
            tokio::time::sleep(Duration::from_millis(delay_ms)).await;
        }
        match relay_client(use_proxy)?.get(&url).send().await {
            Ok(response) => {
                let status = response.status();
                if !status.is_success() {
                    last_protocol_error = Some(format!("Relay 健康端点返回 HTTP {status}"));
                    continue;
                }
                match response.json::<PublicRelayHealth>().await {
                    Ok(health) => return Ok(health),
                    Err(error) => {
                        last_protocol_error = Some(format!("Relay 健康响应格式错误: {error}"));
                    }
                }
            }
            Err(error) => {
                last_transport_error = Some(format!("访问 Relay 健康端点失败: {error}"));
            }
        }
    }
    if let Some(error) = last_protocol_error {
        return Err(AppError::Message(error));
    }
    Err(AppError::Message(
        last_transport_error.unwrap_or_else(|| "访问 Relay 健康端点失败: 未获得响应".into()),
    ))
}

fn relay_probe_is_transport_failure(error: &AppError) -> bool {
    matches!(error, AppError::Message(message) if message.starts_with("访问 Relay 健康端点失败:"))
}

fn relay_client(use_proxy: bool) -> AppResult<reqwest::Client> {
    apply_configured_proxy(
        reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(8))
            .timeout(Duration::from_secs(15)),
        use_proxy,
    )
    .build()
    .map_err(|e| AppError::Message(format!("创建 Relay HTTP 客户端失败: {e}")))
}

fn cloudflare_client(use_proxy: bool) -> AppResult<reqwest::Client> {
    apply_configured_proxy(
        reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(40))
            .user_agent("Coding-Tools-MCP-Relay/0.2.0"),
        use_proxy,
    )
    .build()
    .map_err(|e| AppError::Message(format!("创建 Cloudflare API 客户端失败: {e}")))
}
'''
s = s[:start] + new_region + s[end:]

# Extend the existing relay tests with deterministic proxy-policy checks.
insert = '''

    #[test]
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

    #[test]
    fn relay_service_proxy_switch_is_independent_for_mcp_and_actions() {
        let mut profile = WorkspaceProfile::new("C:/repo".into(), Some("repo".into()));
        profile.tunnel.use_proxy = true;
        profile.actions.use_proxy = false;
        assert!(service_uses_proxy(&profile, TunnelServiceKind::Mcp));
        assert!(!service_uses_proxy(&profile, TunnelServiceKind::Actions));
    }
'''
pos = s.rfind('\n}')
if pos == -1:
    raise SystemExit("relay tests closing brace not found")
s = s[:pos] + insert + s[pos:]
write(p, s)

# ---------------------------------------------------------------------------
# Tauri deployment command: after the remote Worker has been created and local
# config saved, failures in restart/initial sync are warnings, not a false
# "deployment failed" transaction result. The renewal task already retries sync.
# ---------------------------------------------------------------------------
p = Path("app/src-tauri/src/commands/relay.rs")
s = read(p)
s = s.replace('    let result = deploy_result?;\n', '    let mut result = deploy_result?;\n', 1)

old = '''    if let Err(error) = restart_if_running(&state, &id, kind).await {
        return Err(AppError::Message(format!(
            "Relay 已部署到 {}，但当前服务自动重启失败：{error}。Relay 配置已保存；请手动重新启动该服务一次。",
            result.public_url
        )));
    }
'''
new = '''    if let Err(error) = restart_if_running(&state, &id, kind).await {
        result.message.push_str(&format!(
            " 当前服务自动重启失败：{error}。Relay 已部署且配置已保存，请手动重新启动该服务一次。"
        ));
    }
'''
if old not in s:
    raise SystemExit("post-deploy restart block not found")
s = s.replace(old, new, 1)

old = '''    if status.state == "running" && relay::is_valid_quick_target(&status.public_url) {
        let current = profile_by_id(&state, &id)?;
        relay::activate_lease(&current, kind, &status.public_url).await?;
    }

    Ok(result)
'''
new = '''    if status.state == "running" && relay::is_valid_quick_target(&status.public_url) {
        let current = profile_by_id(&state, &id)?;
        if let Err(error) = relay::activate_lease(&current, kind, &status.public_url).await {
            result.message.push_str(&format!(
                " 当前 Quick Tunnel 的首次 Relay 同步暂时失败：{error}。后台续租任务会继续自动重试，无需重启 Tunnel。"
            ));
        }
    }

    Ok(result)
'''
if old not in s:
    raise SystemExit("post-deploy sync block not found")
s = s.replace(old, new, 1)
write(p, s)

print("fix8 relay proxy + deployment transaction semantics applied")
