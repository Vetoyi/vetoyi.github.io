from pathlib import Path

root = Path("app")

def read(path):
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")

def write(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")

# fix6 policy:
# - Keep fix5's "Registered tunnel connection" readiness gate.
# - Revert the speculative 750 ms settle delay.
# - Detect Cloudflare Quick Tunnel 1015/429 explicitly and apply an app-side
#   protective cooldown to stop repeated start/stop loops from hammering the
#   account-less Quick Tunnel allocation endpoint.
# - Health checks use live runtime + live TunnelSupervisor state, never a stale
#   persisted Quick Tunnel URL.
# - Clear persisted Quick Tunnel URLs when a Quick Tunnel stops or fails to start.
# - Show last health-check time in the UI and surface tunnel-start errors.

# ---------------------------------------------------------------------------
# 1) cloudflare.rs: rate-limit detection/cooldown + keep registered gate,
#    remove speculative settle delay.
# ---------------------------------------------------------------------------
p = root / "src-tauri/src/tunnel/cloudflare.rs"
s = read(p)

old = '''use std::path::{Path, PathBuf};
use std::time::Duration;
'''
new = '''use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex as StdMutex};
use std::time::{Duration, Instant};
'''
if old not in s:
    raise SystemExit("cloudflare imports pattern not found")
s = s.replace(old, new, 1)

old = '''const READY_TIMEOUT: Duration = Duration::from_secs(30);
const QUICK_TUNNEL_SETTLE_DELAY: Duration = Duration::from_millis(750);
'''
new = '''const READY_TIMEOUT: Duration = Duration::from_secs(30);
const QUICK_TUNNEL_RATE_LIMIT_STEPS: [u64; 3] = [60, 120, 300];

#[derive(Debug, Default)]
struct QuickTunnelRateLimitState {
    until: Option<Instant>,
    strikes: u8,
}

static QUICK_TUNNEL_RATE_LIMIT: LazyLock<StdMutex<QuickTunnelRateLimitState>> =
    LazyLock::new(|| StdMutex::new(QuickTunnelRateLimitState::default()));

pub fn quick_tunnel_rate_limit_remaining_secs() -> u64 {
    let mut state = QUICK_TUNNEL_RATE_LIMIT
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let Some(until) = state.until else {
        return 0;
    };
    let now = Instant::now();
    if until <= now {
        state.until = None;
        return 0;
    }
    let millis = until.saturating_duration_since(now).as_millis();
    ((millis + 999) / 1000) as u64
}

fn mark_quick_tunnel_rate_limited() -> u64 {
    let mut state = QUICK_TUNNEL_RATE_LIMIT
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    state.strikes = state.strikes.saturating_add(1);
    let index = usize::from(state.strikes.saturating_sub(1))
        .min(QUICK_TUNNEL_RATE_LIMIT_STEPS.len() - 1);
    let seconds = QUICK_TUNNEL_RATE_LIMIT_STEPS[index];
    state.until = Some(Instant::now() + Duration::from_secs(seconds));
    seconds
}

fn clear_quick_tunnel_rate_limit() {
    let mut state = QUICK_TUNNEL_RATE_LIMIT
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    state.until = None;
    state.strikes = 0;
}

fn quick_tunnel_rate_limit_line(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    lower.contains("error code: 1015") || lower.contains("429 too many requests")
}
'''
if old not in s:
    raise SystemExit("fix5 settle constants pattern not found")
s = s.replace(old, new, 1)

marker = '''    if !quick {
        if cloudflare_token.trim().is_empty() {
'''
replacement = '''    if quick {
        let remaining = quick_tunnel_rate_limit_remaining_secs();
        if remaining > 0 {
            return Err(AppError::Message(format!(
                "Cloudflare Quick Tunnel 暂时处于创建冷却期（此前收到 1015 / 429 Too Many Requests）。\\n\\
                 请等待约 {remaining} 秒后再启动；健康检查不会创建 Tunnel，期间无需反复启停服务。"
            )));
        }
    }

    if !quick {
        if cloudflare_token.trim().is_empty() {
'''
if marker not in s:
    raise SystemExit("cloudflare quick gate insertion point not found")
s = s.replace(marker, replacement, 1)

old = '''struct QuickTunnelReady {
    public_url: Option<String>,
    #[allow(dead_code)]
    named_ready: bool,
}
'''
new = '''struct QuickTunnelReady {
    public_url: Option<String>,
    #[allow(dead_code)]
    named_ready: bool,
    error: Option<String>,
}
'''
if old not in s:
    raise SystemExit("QuickTunnelReady struct pattern not found")
s = s.replace(old, new, 1)

s = s.replace(
'''            named_ready: !quick,
        });
''',
'''            named_ready: !quick,
            error: None,
        });
''',
)
s = s.replace(
'''                    named_ready: !quick,
                });
''',
'''                    named_ready: !quick,
                    error: None,
                });
''',
)
old = '''            let _ = sender.send(QuickTunnelReady {
                public_url: url,
                named_ready,
            });
'''
new = '''            let _ = sender.send(QuickTunnelReady {
                public_url: url,
                named_ready,
                error: None,
            });
'''
if old not in s:
    raise SystemExit("send_ready QuickTunnelReady constructor not found")
s = s.replace(old, new, 1)

old = '''    // A Quick Tunnel URL is printed before the connector is necessarily
    // registered at Cloudflare's edge. stream_cloudflare_output now signals
    // readiness only after both conditions are true; keep a small settling
    // window for edge/DNS propagation before exposing the URL to callers.
    if quick {
        time::sleep(QUICK_TUNNEL_SETTLE_DELAY).await;
    }

    let public_url = if quick {
'''
new = '''    if let Some(error) = ready.error {
        let _ = stop_child(child, pid).await;
        return Err(AppError::Message(error));
    }

    let public_url = if quick {
'''
if old not in s:
    raise SystemExit("fix5 settle-delay block not found")
s = s.replace(old, new, 1)

old = '''    let mut handle_line = |line: &str,
                               public_url: &mut Option<String>,
                               ready_tx: &mut Option<oneshot::Sender<QuickTunnelReady>>| {
        if quick {
            if public_url.is_none() {
                if let Some(url) = extract_trycloudflare_url(line) {
                    *public_url = Some(url);
                }
            }
            if cloudflared_connection_registered(line) {
                tunnel_registered = true;
            }
            if let Some(url) = quick_tunnel_ready_url(public_url, tunnel_registered) {
                send_ready(ready_tx, Some(url), true);
            }
        } else {
'''
new = '''    let mut handle_line = |line: &str,
                               public_url: &mut Option<String>,
                               ready_tx: &mut Option<oneshot::Sender<QuickTunnelReady>>| {
        if quick {
            if quick_tunnel_rate_limit_line(line) {
                let cooldown = mark_quick_tunnel_rate_limited();
                if let Some(sender) = ready_tx.take() {
                    let _ = sender.send(QuickTunnelReady {
                        public_url: None,
                        named_ready: false,
                        error: Some(format!(
                            "Cloudflare Quick Tunnel 创建被限流：1015 / 429 Too Many Requests。\\n\\
                             应用已暂停新的 Quick Tunnel 创建约 {cooldown} 秒；期间请不要反复启停 MCP/Actions。"
                        )),
                    });
                }
                return;
            }
            if public_url.is_none() {
                if let Some(url) = extract_trycloudflare_url(line) {
                    *public_url = Some(url);
                }
            }
            if cloudflared_connection_registered(line) {
                tunnel_registered = true;
            }
            if let Some(url) = quick_tunnel_ready_url(public_url, tunnel_registered) {
                clear_quick_tunnel_rate_limit();
                send_ready(ready_tx, Some(url), true);
            }
        } else {
'''
if old not in s:
    raise SystemExit("fix5 handle_line block not found")
s = s.replace(old, new, 1)

s += r'''

#[cfg(test)]
mod fix6_rate_limit_tests {
    use super::quick_tunnel_rate_limit_line;

    #[test]
    fn detects_cloudflare_1015_rate_limit_line() {
        assert!(quick_tunnel_rate_limit_line(
            r#"Error unmarshaling QuickTunnel response: error code: 1015 status_code="429 Too Many Requests""#
        ));
    }

    #[test]
    fn detects_plain_429_rate_limit_line() {
        assert!(quick_tunnel_rate_limit_line("429 Too Many Requests"));
    }

    #[test]
    fn normal_registered_line_is_not_rate_limit() {
        assert!(!quick_tunnel_rate_limit_line(
            "INF Registered tunnel connection connIndex=0 protocol=quic"
        ));
    }
}
'''
write(p, s)

# ---------------------------------------------------------------------------
# 2) Export rate-limit state to health command.
# ---------------------------------------------------------------------------
p = root / "src-tauri/src/tunnel/mod.rs"
s = read(p)
old = '''pub use cloudflare::{
    extract_trycloudflare_url, resolve_cloudflared, spawn_cloudflare_tunnel, stop_child,
};
'''
new = '''pub use cloudflare::{
    extract_trycloudflare_url, quick_tunnel_rate_limit_remaining_secs, resolve_cloudflared,
    spawn_cloudflare_tunnel, stop_child,
};
'''
if old not in s:
    raise SystemExit("tunnel/mod.rs cloudflare export pattern not found")
s = s.replace(old, new, 1)
write(p, s)

# ---------------------------------------------------------------------------
# 3) Health checker: use LIVE runtime/tunnel context, never persisted Quick URL.
# ---------------------------------------------------------------------------
p = root / "src-tauri/src/health/checker.rs"
s = read(p)

old = '''            "Quick Tunnel 已由外部客户端实际连接成功时，这通常是本机代理/DNS/回环路径问题，不代表对应服务故障。",
'''
new = '''            "当前 Tunnel Supervisor 报告 Quick Tunnel 正在运行，但本机公网 HTTP 探测失败。可重复运行健康检查重新探测同一当前 URL；无需通过反复启停服务来刷新结果。",
'''
if old not in s:
    raise SystemExit("fix3 warning wording not found")
s = s.replace(old, new, 1)

marker = '''pub struct HealthItem {
    pub label: String,
    pub ok: bool,
    pub status: String,
    pub detail: String,
    pub hint: String,
}
'''
insert = '''pub struct HealthItem {
    pub label: String,
    pub ok: bool,
    pub status: String,
    pub detail: String,
    pub hint: String,
}

#[derive(Debug, Clone, Default)]
pub struct HealthRuntimeContext {
    pub mcp_running: bool,
    pub actions_running: bool,
    pub mcp_tunnel_running: bool,
    pub mcp_public_url: String,
    pub actions_tunnel_running: bool,
    pub actions_public_url: String,
    pub quick_tunnel_rate_limit_remaining_secs: u64,
}
'''
if marker not in s:
    raise SystemExit("HealthItem block not found")
s = s.replace(marker, insert, 1)

marker = '''pub async fn run_health_checks(profile: &WorkspaceProfile) -> Vec<HealthItem> {
'''
if marker not in s:
    raise SystemExit("run_health_checks signature not found")

helpers = r'''fn tunnel_configured(tunnel_type: &str) -> bool {
    let value = tunnel_type.trim();
    !value.is_empty() && value != "none"
}

fn quick_cloudflare(tunnel_type: &str, mode: &str) -> bool {
    tunnel_type == "cloudflare" && mode != "named"
}

fn tunnel_not_running_detail(
    service: &str,
    tunnel_type: &str,
    cloudflare_mode: &str,
    quick_cooldown_secs: u64,
) -> String {
    if quick_cloudflare(tunnel_type, cloudflare_mode) && quick_cooldown_secs > 0 {
        return format!(
            "{service} Quick Tunnel 未运行：Cloudflare 最近返回 1015 / 429 Too Many Requests。\
             应用正在保护性冷却，约 {quick_cooldown_secs} 秒后才能再次创建。\
             健康检查只读取当前状态，不会创建或重启 Tunnel；请不要反复启停服务。"
        );
    }
    format!(
        "{service} 公网隧道当前未运行。健康检查只读取实时状态，不会使用上一次保存的 Quick Tunnel URL，\
         也不会自动创建/重启 Tunnel。请查看服务启动提示或 cloudflared 日志。"
    )
}

fn public_endpoint(base: &str, suffix: &str) -> String {
    if base.trim().is_empty() {
        String::new()
    } else {
        format!("{}{}", base.trim().trim_end_matches('/'), suffix)
    }
}

'''
s = s.replace(marker, helpers + marker, 1)

start = s.find('pub async fn run_health_checks(profile: &WorkspaceProfile) -> Vec<HealthItem> {')
end = s.find('\n#[cfg(test)]', start)
if start < 0 or end < 0:
    raise SystemExit("health run function boundaries not found")

new_run = r'''pub async fn run_health_checks(
    profile: &WorkspaceProfile,
    context: &HealthRuntimeContext,
) -> Vec<HealthItem> {
    let local_client = http_client(false);
    let mcp_public_client = http_client(profile.tunnel.use_proxy);
    let actions_public_client = http_client(profile.actions.use_proxy);
    let mut items = Vec::new();

    if !context.mcp_running {
        for label in [
            "本地 /mcp",
            "公网 /mcp",
            "MCP OAuth 授权元数据",
            "MCP OAuth 受保护资源",
        ] {
            items.push(skipped_item(label, "MCP 未运行（已跳过）"));
        }
    } else {
        let (mcp_local_ok, mcp_local_detail) =
            check_url(&local_client, &profile.local_endpoint()).await;
        items.push(health_item(
            "本地 /mcp",
            mcp_local_ok,
            mcp_local_detail,
            "Runtime 显示 MCP 正在运行，但本地 endpoint 未正常响应。请查看 MCP 日志。",
        ));

        if !mcp_local_ok {
            for label in [
                "公网 /mcp",
                "MCP OAuth 授权元数据",
                "MCP OAuth 受保护资源",
            ] {
                items.push(skipped_item(label, "本地 MCP 未正常响应（已跳过公网检查）"));
            }
        } else if !tunnel_configured(&profile.tunnel.tunnel_type) {
            for label in [
                "公网 /mcp",
                "MCP OAuth 授权元数据",
                "MCP OAuth 受保护资源",
            ] {
                items.push(skipped_item(label, "MCP 公网隧道未配置（已跳过）"));
            }
        } else if !context.mcp_tunnel_running {
            let detail = tunnel_not_running_detail(
                "MCP",
                &profile.tunnel.tunnel_type,
                &profile.tunnel.cloudflare_mode,
                context.quick_tunnel_rate_limit_remaining_secs,
            );
            items.push(health_item(
                "公网 /mcp",
                false,
                detail.clone(),
                "先处理隧道启动失败原因；健康检查不会通过重试自动创建新的 Quick Tunnel。",
            ));
            if profile.auth.auth_type == "oauth" {
                items.push(health_item(
                    "MCP OAuth 授权元数据",
                    false,
                    detail.clone(),
                    "公网隧道未运行，因此无法验证公网 OAuth 元数据。",
                ));
                items.push(health_item(
                    "MCP OAuth 受保护资源",
                    false,
                    detail,
                    "公网隧道未运行，因此无法验证公网受保护资源元数据。",
                ));
            } else {
                items.push(skipped_item(
                    "MCP OAuth 授权元数据",
                    "MCP 未启用 OAuth（已跳过）",
                ));
                items.push(skipped_item(
                    "MCP OAuth 受保护资源",
                    "MCP 未启用 OAuth（已跳过）",
                ));
            }
        } else {
            let mcp_public = context.mcp_public_url.trim().trim_end_matches('/').to_string();
            if mcp_public.is_empty() {
                let detail = "Tunnel Supervisor 报告 MCP 隧道正在运行，但没有当前公网 URL。".to_string();
                items.push(health_item(
                    "公网 /mcp",
                    false,
                    detail.clone(),
                    "这是运行状态不一致，请停止后只启动一次 MCP，并查看 cloudflared 日志。",
                ));
                items.push(health_item(
                    "MCP OAuth 授权元数据",
                    false,
                    detail.clone(),
                    "缺少当前公网 URL，无法验证 OAuth。",
                ));
                items.push(health_item(
                    "MCP OAuth 受保护资源",
                    false,
                    detail,
                    "缺少当前公网 URL，无法验证 OAuth。",
                ));
            } else {
                let endpoint = public_endpoint(&mcp_public, "/mcp");
                let mut mcp_public_result =
                    check_mcp_public_url(&mcp_public_client, &endpoint).await;
                mcp_public_result.1 =
                    format!("{}; 当前实时 Tunnel={}", mcp_public_result.1, mcp_public);
                items.push(public_health_item(
                    "公网 /mcp",
                    mcp_public_result.0,
                    mcp_public_result.1,
                    "当前实时隧道存在，但公网 endpoint 响应异常；请查看 HTTP 状态和 cloudflared 日志。",
                    mcp_local_ok,
                    &mcp_public,
                ));

                if profile.auth.auth_type == "oauth" {
                    let oauth_metadata_url =
                        well_known_url(&mcp_public, ".well-known/oauth-authorization-server");
                    let protected_resource_url =
                        well_known_url(&mcp_public, ".well-known/oauth-protected-resource");
                    let (mcp_oauth_result, mcp_protected_result) = tokio::join!(
                        check_oauth_metadata(&mcp_public_client, &oauth_metadata_url, &mcp_public),
                        check_protected_resource(
                            &mcp_public_client,
                            &protected_resource_url,
                            &mcp_public
                        )
                    );
                    items.push(public_health_item(
                        "MCP OAuth 授权元数据",
                        mcp_oauth_result.0,
                        mcp_oauth_result.1,
                        "OAuth issuer / authorization_endpoint / token_endpoint 必须与当前实时 Tunnel URL 一致。",
                        mcp_local_ok,
                        &mcp_public,
                    ));
                    items.push(public_health_item(
                        "MCP OAuth 受保护资源",
                        mcp_protected_result.0,
                        mcp_protected_result.1,
                        "resource 与 authorization_servers 必须与当前实时 Tunnel URL 一致。",
                        mcp_local_ok,
                        &mcp_public,
                    ));
                } else {
                    items.push(skipped_item(
                        "MCP OAuth 授权元数据",
                        "MCP 未启用 OAuth（已跳过）",
                    ));
                    items.push(skipped_item(
                        "MCP OAuth 受保护资源",
                        "MCP 未启用 OAuth（已跳过）",
                    ));
                }
            }
        }
    }

    if !context.actions_running {
        for label in [
            "本地 Actions /health",
            "本地 Actions /openapi.json",
            "公网 Actions /openapi.json",
            "Actions OAuth 授权元数据",
        ] {
            items.push(skipped_item(label, "Actions 未运行（已跳过）"));
        }
        return items;
    }

    let actions_local = profile.actions_local_base_url();
    let actions_health_url = format!("{actions_local}/health");
    let (actions_local_ok, actions_local_detail) =
        check_url(&local_client, &actions_health_url).await;
    items.push(health_item(
        "本地 Actions /health",
        actions_local_ok,
        actions_local_detail,
        "Runtime 显示 Actions 正在运行，但本地 /health 未正常响应。请查看 actions-stderr.log。",
    ));

    if !actions_local_ok {
        for label in [
            "本地 Actions /openapi.json",
            "公网 Actions /openapi.json",
            "Actions OAuth 授权元数据",
        ] {
            items.push(skipped_item(
                label,
                "本地 Actions 未正常响应（已跳过后续检查）",
            ));
        }
        return items;
    }

    let actions_openapi_local = format!("{actions_local}/openapi.json");
    let actions_openapi_local_result =
        check_openapi_server(&local_client, &actions_openapi_local, &actions_local).await;
    items.push(health_item(
        "本地 Actions /openapi.json",
        actions_openapi_local_result.0,
        actions_openapi_local_result.1,
        "本地 OpenAPI servers 必须指向当前本地 Actions 地址。",
    ));

    if !tunnel_configured(&profile.actions.tunnel_type) {
        items.push(skipped_item(
            "公网 Actions /openapi.json",
            "Actions 公网隧道未配置（已跳过）",
        ));
        items.push(skipped_item(
            "Actions OAuth 授权元数据",
            if profile.actions.auth_type == "oauth" {
                "Actions 公网隧道未配置（已跳过公网 OAuth 检查）"
            } else {
                "Actions 未启用 OAuth（已跳过）"
            },
        ));
        return items;
    }

    if !context.actions_tunnel_running {
        let detail = tunnel_not_running_detail(
            "Actions",
            &profile.actions.tunnel_type,
            &profile.actions.cloudflare_mode,
            context.quick_tunnel_rate_limit_remaining_secs,
        );
        items.push(health_item(
            "公网 Actions /openapi.json",
            false,
            detail.clone(),
            "先处理隧道启动失败原因；健康检查不会通过重试自动创建新的 Quick Tunnel。",
        ));
        if profile.actions.auth_type == "oauth" {
            items.push(health_item(
                "Actions OAuth 授权元数据",
                false,
                detail,
                "公网隧道未运行，因此无法验证公网 OAuth 元数据。",
            ));
        } else {
            items.push(skipped_item(
                "Actions OAuth 授权元数据",
                "Actions 未启用 OAuth（已跳过）",
            ));
        }
        return items;
    }

    let actions_public = context
        .actions_public_url
        .trim()
        .trim_end_matches('/')
        .to_string();
    if actions_public.is_empty() {
        let detail =
            "Tunnel Supervisor 报告 Actions 隧道正在运行，但没有当前公网 URL。".to_string();
        items.push(health_item(
            "公网 Actions /openapi.json",
            false,
            detail.clone(),
            "这是运行状态不一致，请停止后只启动一次 Actions，并查看 cloudflared 日志。",
        ));
        if profile.actions.auth_type == "oauth" {
            items.push(health_item(
                "Actions OAuth 授权元数据",
                false,
                detail,
                "缺少当前公网 URL，无法验证 OAuth。",
            ));
        } else {
            items.push(skipped_item(
                "Actions OAuth 授权元数据",
                "Actions 未启用 OAuth（已跳过）",
            ));
        }
        return items;
    }

    let actions_openapi_public = public_endpoint(&actions_public, "/openapi.json");
    let actions_openapi_public_result = check_openapi_server(
        &actions_public_client,
        &actions_openapi_public,
        &actions_public,
    )
    .await;
    items.push(public_health_item(
        "公网 Actions /openapi.json",
        actions_openapi_public_result.0,
        actions_openapi_public_result.1,
        "当前实时隧道存在，但公网 OpenAPI 响应异常；servers 必须指向当前实时 Tunnel URL。",
        actions_local_ok,
        &actions_public,
    ));

    if profile.actions.auth_type == "oauth" {
        let actions_oauth_url =
            well_known_url(&actions_public, ".well-known/oauth-authorization-server");
        let actions_oauth_result =
            check_oauth_metadata(&actions_public_client, &actions_oauth_url, &actions_public).await;
        items.push(public_health_item(
            "Actions OAuth 授权元数据",
            actions_oauth_result.0,
            actions_oauth_result.1,
            "Actions OAuth 元数据必须与当前实时 Tunnel URL 一致。",
            actions_local_ok,
            &actions_public,
        ));
    } else {
        items.push(skipped_item(
            "Actions OAuth 授权元数据",
            "Actions 未启用 OAuth（已跳过）",
        ));
    }

    items
}
'''
s = s[:start] + new_run + s[end:]

needle = '\n    #[test]\n    fn only_network_errors_are_downgraded_to_warning() {'
tests = r'''
    #[test]
    fn quick_cloudflare_is_detected_for_health_state() {
        assert!(super::quick_cloudflare("cloudflare", "quick"));
        assert!(!super::quick_cloudflare("cloudflare", "named"));
        assert!(!super::quick_cloudflare("frp", "quick"));
    }

    #[test]
    fn stopped_tunnel_message_mentions_rate_limit_when_cooling_down() {
        let detail = super::tunnel_not_running_detail(
            "MCP",
            "cloudflare",
            "quick",
            42,
        );
        assert!(detail.contains("1015 / 429"));
        assert!(detail.contains("42"));
        assert!(detail.contains("健康检查只读取当前状态"));
    }

'''
if needle not in s:
    raise SystemExit("health test insertion point not found for fix6")
s = s.replace(needle, tests + needle, 1)
write(p, s)

p = root / "src-tauri/src/health/mod.rs"
s = read(p)
old = 'pub use checker::{run_health_checks, HealthItem};'
new = 'pub use checker::{run_health_checks, HealthItem, HealthRuntimeContext};'
if old not in s:
    raise SystemExit("health/mod.rs export pattern not found")
s = s.replace(old, new, 1)
write(p, s)

# ---------------------------------------------------------------------------
# 4) Health Tauri command: capture live runtime + live TunnelSupervisor status.
# ---------------------------------------------------------------------------
p = root / "src-tauri/src/commands/health.rs"
s = r'''use tauri::State;

use crate::app_state::AppState;
use crate::error::{AppError, AppResult};
use crate::health::{
    run_health_checks as execute_health_checks, HealthItem, HealthRuntimeContext,
};
use crate::runtime::ServiceKind;
use crate::tunnel::{
    quick_tunnel_rate_limit_remaining_secs, supervisor, TunnelServiceKind,
};

fn profile_by_id(state: &AppState, id: &str) -> AppResult<crate::workspace::WorkspaceProfile> {
    state.with_workspaces(|store| {
        store
            .get(id)
            .cloned()
            .ok_or_else(|| AppError::Message(format!("workspace not found: {id}")))
    })
}

#[tauri::command]
pub async fn run_health_checks(
    state: State<'_, AppState>,
    id: String,
) -> AppResult<Vec<HealthItem>> {
    let profile = profile_by_id(&state, &id)?;

    let (mcp_running, actions_running) = state.with_runtime(|runtime| {
        Ok((
            runtime.is_running(&id, ServiceKind::Mcp),
            runtime.is_running(&id, ServiceKind::Actions),
        ))
    })?;

    let settings = state.with_settings(|store| Ok(store.settings()))?;
    let (mcp_tunnel, actions_tunnel) = {
        let guard = supervisor().lock().await;
        (
            guard.status(&profile, TunnelServiceKind::Mcp, &settings),
            guard.status(&profile, TunnelServiceKind::Actions, &settings),
        )
    };

    let context = HealthRuntimeContext {
        mcp_running,
        actions_running,
        mcp_tunnel_running: mcp_tunnel.state == "running",
        mcp_public_url: if mcp_tunnel.state == "running" {
            mcp_tunnel.public_url
        } else {
            String::new()
        },
        actions_tunnel_running: actions_tunnel.state == "running",
        actions_public_url: if actions_tunnel.state == "running" {
            actions_tunnel.public_url
        } else {
            String::new()
        },
        quick_tunnel_rate_limit_remaining_secs: quick_tunnel_rate_limit_remaining_secs(),
    };

    Ok(execute_health_checks(&profile, &context).await)
}
'''
write(p, s)

# ---------------------------------------------------------------------------
# 5) Runtime lifecycle: clear stale Quick URLs and surface tunnel-start error.
# ---------------------------------------------------------------------------
p = root / "src-tauri/src/commands/runtime.rs"
s = read(p)

old = '''    if url.is_empty() {
        return Ok(());
    }

    state.with_workspaces(|store| {
'''
new = '''    state.with_workspaces(|store| {
'''
if old not in s:
    raise SystemExit("runtime persist_tunnel_url empty guard not found")
s = s.replace(old, new, 1)

marker = '''async fn sync_tunnel_routes_from_runtime(state: &AppState) -> AppResult<()> {
'''
helper = '''fn is_quick_cloudflare(
    profile: &crate::workspace::WorkspaceProfile,
    kind: TunnelServiceKind,
) -> bool {
    match kind {
        TunnelServiceKind::Mcp => {
            profile.tunnel.tunnel_type == "cloudflare"
                && profile.tunnel.cloudflare_mode != "named"
        }
        TunnelServiceKind::Actions => {
            profile.actions.tunnel_type == "cloudflare"
                && profile.actions.cloudflare_mode != "named"
        }
    }
}

async fn sync_tunnel_routes_from_runtime(state: &AppState) -> AppResult<()> {
'''
if marker not in s:
    raise SystemExit("runtime helper insertion point not found")
s = s.replace(marker, helper, 1)

old = '''    stop_for_runtime(&profile, TunnelServiceKind::Mcp).await?;
    sync_tunnel_routes_from_runtime(state).await?;
'''
new = '''    stop_for_runtime(&profile, TunnelServiceKind::Mcp).await?;
    if is_quick_cloudflare(&profile, TunnelServiceKind::Mcp) {
        persist_tunnel_url(state, id, TunnelServiceKind::Mcp, "")?;
    }
    sync_tunnel_routes_from_runtime(state).await?;
'''
if old not in s:
    raise SystemExit("stop_mcp stale-url clear pattern not found")
s = s.replace(old, new, 1)

old = '''    stop_for_runtime(&profile, TunnelServiceKind::Actions).await?;
    sync_tunnel_routes_from_runtime(state).await?;
'''
new = '''    stop_for_runtime(&profile, TunnelServiceKind::Actions).await?;
    if is_quick_cloudflare(&profile, TunnelServiceKind::Actions) {
        persist_tunnel_url(state, id, TunnelServiceKind::Actions, "")?;
    }
    sync_tunnel_routes_from_runtime(state).await?;
'''
if old not in s:
    raise SystemExit("stop_actions stale-url clear pattern not found")
s = s.replace(old, new, 1)

old = '''    match maybe_start_for_runtime(&profile, TunnelServiceKind::Mcp).await {
        Ok(Some(url)) => {
            persist_tunnel_url(state, id, TunnelServiceKind::Mcp, &url)?;
        }
        Ok(None) => {}
        Err(error) => {
            eprintln!("mcp tunnel auto-start failed for {id}: {error}");
        }
    }

    let profile = profile_by_id(state, id)?;
    tokio::time::sleep(Duration::from_millis(250)).await;
    state.with_runtime(|runtime| {
        runtime.refresh_mcp(&profile);
        Ok(runtime.mcp_status(&profile))
    })
'''
new = '''    let mut tunnel_error = None;
    match maybe_start_for_runtime(&profile, TunnelServiceKind::Mcp).await {
        Ok(Some(url)) => {
            persist_tunnel_url(state, id, TunnelServiceKind::Mcp, &url)?;
        }
        Ok(None) => {}
        Err(error) => {
            eprintln!("mcp tunnel auto-start failed for {id}: {error}");
            tunnel_error = Some(error.to_string());
            if is_quick_cloudflare(&profile, TunnelServiceKind::Mcp) {
                persist_tunnel_url(state, id, TunnelServiceKind::Mcp, "")?;
            }
        }
    }

    let profile = profile_by_id(state, id)?;
    tokio::time::sleep(Duration::from_millis(250)).await;
    let mut status = state.with_runtime(|runtime| {
        runtime.refresh_mcp(&profile);
        Ok(runtime.mcp_status(&profile))
    })?;
    if let Some(error) = tunnel_error {
        status.public_endpoint.clear();
        status.public_message = format!("公网隧道启动失败：{error}");
    }
    Ok(status)
'''
if old not in s:
    raise SystemExit("start_mcp tunnel block not found")
s = s.replace(old, new, 1)

old = '''    match maybe_start_for_runtime(&profile, TunnelServiceKind::Actions).await {
        Ok(Some(url)) => {
            persist_tunnel_url(state, id, TunnelServiceKind::Actions, &url)?;
        }
        Ok(None) => {}
        Err(error) => {
            eprintln!("actions tunnel auto-start failed for {id}: {error}");
        }
    }

    let profile = profile_by_id(state, id)?;
    tokio::time::sleep(Duration::from_millis(250)).await;
    state.with_runtime(|runtime| {
        runtime.refresh_actions(&profile);
        Ok(runtime.actions_status(&profile))
    })
'''
new = '''    let mut tunnel_error = None;
    match maybe_start_for_runtime(&profile, TunnelServiceKind::Actions).await {
        Ok(Some(url)) => {
            persist_tunnel_url(state, id, TunnelServiceKind::Actions, &url)?;
        }
        Ok(None) => {}
        Err(error) => {
            eprintln!("actions tunnel auto-start failed for {id}: {error}");
            tunnel_error = Some(error.to_string());
            if is_quick_cloudflare(&profile, TunnelServiceKind::Actions) {
                persist_tunnel_url(state, id, TunnelServiceKind::Actions, "")?;
            }
        }
    }

    let profile = profile_by_id(state, id)?;
    tokio::time::sleep(Duration::from_millis(250)).await;
    let mut status = state.with_runtime(|runtime| {
        runtime.refresh_actions(&profile);
        Ok(runtime.actions_status(&profile))
    })?;
    if let Some(error) = tunnel_error {
        status.public_endpoint.clear();
        status.public_message = format!("公网隧道启动失败：{error}");
    }
    Ok(status)
'''
if old not in s:
    raise SystemExit("start_actions tunnel block not found")
s = s.replace(old, new, 1)
write(p, s)

# ---------------------------------------------------------------------------
# 6) Direct tunnel commands: clear persisted Quick URL on stop/failed start.
# ---------------------------------------------------------------------------
p = root / "src-tauri/src/commands/tunnel.rs"
s = read(p)

old = '''    if public_url.is_empty() {
        return Ok(());
    }
    state.with_workspaces(|store| {
'''
new = '''    state.with_workspaces(|store| {
'''
if old not in s:
    raise SystemExit("commands/tunnel persist_public_url guard not found")
s = s.replace(old, new, 1)

marker = '''fn tunnel_type_for(profile: &crate::workspace::WorkspaceProfile, kind: TunnelServiceKind) -> &str {
'''
helper = '''fn is_quick_cloudflare(
    profile: &crate::workspace::WorkspaceProfile,
    kind: TunnelServiceKind,
) -> bool {
    match kind {
        TunnelServiceKind::Mcp => {
            profile.tunnel.tunnel_type == "cloudflare"
                && profile.tunnel.cloudflare_mode != "named"
        }
        TunnelServiceKind::Actions => {
            profile.actions.tunnel_type == "cloudflare"
                && profile.actions.cloudflare_mode != "named"
        }
    }
}

fn tunnel_type_for(profile: &crate::workspace::WorkspaceProfile, kind: TunnelServiceKind) -> &str {
'''
if marker not in s:
    raise SystemExit("commands/tunnel helper insertion point not found")
s = s.replace(marker, helper, 1)

old = '''            }
            return Err(error);
        }
    };

    persist_public_url(&state, &id, kind, &status.public_url)?;
'''
new = '''            }
            if is_quick_cloudflare(&profile, kind) {
                let _ = persist_public_url(&state, &id, kind, "");
            }
            return Err(error);
        }
    };

    persist_public_url(&state, &id, kind, &status.public_url)?;
'''
if old not in s:
    raise SystemExit("restart_tunnel error branch pattern not found")
s = s.replace(old, new, 1)

old = '''    let status = {
        let mut guard = supervisor().lock().await;
        guard.start(&profile, kind, &settings).await?
    };

    persist_public_url(&state, &id, kind, &status.public_url)?;
    Ok(status)
'''
new = '''    let status = {
        let mut guard = supervisor().lock().await;
        guard.start(&profile, kind, &settings).await
    };

    let status = match status {
        Ok(status) => status,
        Err(error) => {
            if is_quick_cloudflare(&profile, kind) {
                let _ = persist_public_url(&state, &id, kind, "");
            }
            return Err(error);
        }
    };

    persist_public_url(&state, &id, kind, &status.public_url)?;
    Ok(status)
'''
if old not in s:
    raise SystemExit("start_tunnel explicit error pattern not found")
s = s.replace(old, new, 1)

old = '''    let mut guard = supervisor().lock().await;
    guard.stop(&profile, kind, &settings).await?;
    Ok(guard.status(&profile, kind, &settings))
'''
new = '''    let mut guard = supervisor().lock().await;
    guard.stop(&profile, kind, &settings).await?;
    let mut status = guard.status(&profile, kind, &settings);
    drop(guard);
    if is_quick_cloudflare(&profile, kind) {
        persist_public_url(&state, &id, kind, "")?;
        status.public_url.clear();
    }
    Ok(status)
'''
if old not in s:
    raise SystemExit("stop_tunnel stale-url pattern not found")
s = s.replace(old, new, 1)
write(p, s)

# ---------------------------------------------------------------------------
# 7) Frontend: detailed start warning + visible health rerun timestamp.
# ---------------------------------------------------------------------------
p = root / "src/routes/workspaces/[id]/+page.svelte"
s = read(p)

old = '''    runtime: { state: RuntimeState; publicEndpoint: string },
'''
new = '''    runtime: { state: RuntimeState; publicEndpoint: string; publicMessage?: string },
'''
if old not in s:
    raise SystemExit("afterServiceStart runtime type pattern not found")
s = s.replace(old, new, 1)

old = '''      showToast(
        "本地服务已启动，但隧道未能自动连接。请检查代理设置与隧道配置，或查看日志。",
        { title: "隧道未连接", kind: "warning", duration: 8000 },
      );
'''
new = '''      const detail =
        runtime.publicMessage && runtime.publicMessage !== "未知"
          ? runtime.publicMessage
          : "本地服务已启动，但公网隧道未连接。请查看健康检查或 cloudflared 日志。";
      showToast(detail, {
        title: "公网隧道未连接",
        kind: "warning",
        duration: 12000,
      });
'''
if old not in s:
    raise SystemExit("afterServiceStart toast pattern not found")
s = s.replace(old, new, 1)
write(p, s)

p = root / "src/lib/components/HealthPanel.svelte"
s = read(p)

old = '''  let busy = $state(false);
  let error = $state("");
'''
new = '''  let busy = $state(false);
  let error = $state("");
  let lastCheckedAt = $state("");
  let checkCount = $state(0);
'''
if old not in s:
    raise SystemExit("HealthPanel state pattern not found")
s = s.replace(old, new, 1)

old = '''      items = onRunCheck ? await onRunCheck(workspaceId) : await runHealthChecks(workspaceId);
'''
new = '''      items = onRunCheck ? await onRunCheck(workspaceId) : await runHealthChecks(workspaceId);
      checkCount += 1;
      lastCheckedAt = new Date().toLocaleTimeString();
'''
if old not in s:
    raise SystemExit("HealthPanel run result pattern not found")
s = s.replace(old, new, 1)

old = '''      <p class="mt-1 text-sm text-[var(--color-text-muted)]">
        MCP、Actions 本地/公网 endpoint 与 OAuth 元数据
      </p>
'''
new = '''      <p class="mt-1 text-sm text-[var(--color-text-muted)]">
        MCP、Actions 本地/公网 endpoint 与 OAuth 元数据
      </p>
      {#if lastCheckedAt}
        <p class="mt-1 text-xs text-[var(--color-text-muted)]">
          最近检查：{lastCheckedAt}（本页第 {checkCount} 次）
        </p>
      {/if}
'''
if old not in s:
    raise SystemExit("HealthPanel subtitle pattern not found")
s = s.replace(old, new, 1)
write(p, s)

print("fix6 lifecycle/health transformations applied")
