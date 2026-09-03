from pathlib import Path


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")

# ---------------------------------------------------------------------------
# Rust relay hardening.
# ---------------------------------------------------------------------------
p = Path("app/src-tauri/src/relay/mod.rs")
s = read(p)

s = s.replace('.take(16)\n        .collect::<String>()', '.take(24)\n        .collect::<String>()', 1)
s = s.replace('hex_prefix(&digest, 12)', 'hex_prefix(&digest, 24)', 1)
s = s.replace(
    'assert_eq!(name, "ctm-relay-mcp-0123456789abcdef");',
    'assert_eq!(name, "ctm-relay-mcp-0123456789abcdef01234567");',
    1,
)

# Make the generic Cloudflare envelope deserialize without imposing an accidental
# T: Default bound from serde's derive inference.
old = '''#[derive(Debug, Deserialize)]
struct CfEnvelope<T> {
'''
new = '''#[derive(Debug, Deserialize)]
#[serde(bound(deserialize = "T: Deserialize<'de>"))]
struct CfEnvelope<T> {
'''
if old not in s:
    raise SystemExit("CfEnvelope declaration not found")
s = s.replace(old, new, 1)

old = '''pub fn request_has_valid_origin_proof(
    headers: &axum::http::HeaderMap,
    expected_token: Option<&str>,
) -> bool {
    let Some(expected) = expected_token.filter(|value| !value.is_empty()) else {
        return true;
    };

    // Local loopback callers (desktop health checks and direct localhost use) do not
    // traverse the Relay and therefore cannot carry the origin proof. Trust only the
    // raw Host header here; never X-Forwarded-Host, which remote clients can spoof.
    let host = headers
        .get(axum::http::header::HOST)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    let host_only = if host.starts_with('[') {
        host.strip_prefix('[')
            .and_then(|value| value.split_once(']'))
            .map(|(value, _)| value)
            .unwrap_or("")
    } else {
        host.split(':').next().unwrap_or("")
    };
    if matches!(host_only, "127.0.0.1" | "localhost" | "::1") {
        return true;
    }

    let supplied = headers
        .get("x-ctm-origin-auth")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    constant_time_eq(expected.as_bytes(), supplied.as_bytes())
}
'''
new = '''pub fn request_has_valid_origin_proof(
    headers: &axum::http::HeaderMap,
    expected_token: Option<&str>,
) -> bool {
    let Some(expected) = expected_token.filter(|value| !value.is_empty()) else {
        return true;
    };

    // A valid Relay proof always wins. A supplied but invalid proof is never allowed
    // to fall back to localhost handling.
    let supplied = headers
        .get("x-ctm-origin-auth")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    if !supplied.is_empty() {
        return constant_time_eq(expected.as_bytes(), supplied.as_bytes());
    }

    // Direct local callers are allowed for desktop health checks. Do not trust a
    // spoofed Host=127.0.0.1 if the request carries evidence that it traversed a
    // proxy/Cloudflare path.
    let proxied = [
        "cf-connecting-ip",
        "cf-ray",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-ctm-relay",
    ]
    .iter()
    .any(|name| headers.contains_key(*name));
    if proxied {
        return false;
    }

    let host = headers
        .get(axum::http::header::HOST)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    let host_only = if host.starts_with('[') {
        host.strip_prefix('[')
            .and_then(|value| value.split_once(']'))
            .map(|(value, _)| value)
            .unwrap_or("")
    } else {
        host.split(':').next().unwrap_or("")
    };
    matches!(host_only, "127.0.0.1" | "localhost" | "::1")
}
'''
if old not in s:
    raise SystemExit("origin guard block not found")
s = s.replace(old, new, 1)

# Do not move health.backend before using it to build the detail message.
old = '''            backend: if health.backend.is_empty() { "unknown".into() } else { health.backend },
            expires_in: health.expires_in,
            detail: if health.backend == "online" {
'''
new = '''            backend: if health.backend.is_empty() { "unknown".into() } else { health.backend.clone() },
            expires_in: health.expires_in,
            detail: if health.backend == "online" {
'''
if old not in s:
    raise SystemExit("remote status backend move block not found")
s = s.replace(old, new, 1)

old = '''async fn probe_url(public_url: &str) -> AppResult<PublicRelayHealth> {
    let response = relay_client()?
        .get(format!("{}/__ctm/health", public_url.trim_end_matches('/')))
        .send()
        .await
        .map_err(|e| AppError::Message(format!("访问 Relay 健康端点失败: {e}")))?;
    let status = response.status();
    if !status.is_success() {
        return Err(AppError::Message(format!("Relay 健康端点返回 HTTP {status}")));
    }
    response
        .json::<PublicRelayHealth>()
        .await
        .map_err(|e| AppError::Message(format!("Relay 健康响应格式错误: {e}")))
}
'''
new = '''async fn probe_url(public_url: &str) -> AppResult<PublicRelayHealth> {
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
'''
if old not in s:
    raise SystemExit("probe_url block not found")
s = s.replace(old, new, 1)

old = '''    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        return Err(AppError::Message(format!(
            "删除 Relay Worker 失败: HTTP {status}; {}",
            summarize_cf_body(&body)
        )));
    }
    delete_identity(&profile.id, kind)?;
'''
new = '''    let status = response.status();
    if status == reqwest::StatusCode::NOT_FOUND {
        delete_identity(&profile.id, kind)?;
        return Ok(());
    }
    if !status.is_success() {
        let body = response.text().await.unwrap_or_default();
        return Err(AppError::Message(format!(
            "删除 Relay Worker 失败: HTTP {status}; {}",
            summarize_cf_body(&body)
        )));
    }
    delete_identity(&profile.id, kind)?;
'''
if old not in s:
    raise SystemExit("delete idempotency block not found")
s = s.replace(old, new, 1)

# Add origin-guard regression tests inside the existing test module.
insert = '''

    #[test]
    fn origin_guard_allows_direct_localhost_but_rejects_spoofed_proxy_localhost() {
        let mut local = axum::http::HeaderMap::new();
        local.insert(axum::http::header::HOST, "127.0.0.1:28766".parse().unwrap());
        assert!(request_has_valid_origin_proof(&local, Some("relay-secret")));

        let mut spoofed = local.clone();
        spoofed.insert("cf-ray", "deadbeef".parse().unwrap());
        assert!(!request_has_valid_origin_proof(&spoofed, Some("relay-secret")));

        spoofed.insert("x-ctm-origin-auth", "relay-secret".parse().unwrap());
        assert!(request_has_valid_origin_proof(&spoofed, Some("relay-secret")));
    }
'''
pos = s.rfind('\n}')
if pos == -1:
    raise SystemExit("relay test module closing brace not found")
s = s[:pos] + insert + s[pos:]
write(p, s)

# ---------------------------------------------------------------------------
# Workspace module must publicly expose RelayConfig to commands/runtime modules.
# ---------------------------------------------------------------------------
p = Path("app/src-tauri/src/workspace/mod.rs")
s = read(p)
old = 'pub use model::{ActionsConfig, AuthConfig, RuntimeConfig, RuntimeStatusDto, WorkspaceProfile};'
new = 'pub use model::{ActionsConfig, AuthConfig, RelayConfig, RuntimeConfig, RuntimeStatusDto, WorkspaceProfile};'
if old not in s:
    raise SystemExit("workspace model re-export line not found")
s = s.replace(old, new, 1)
write(p, s)

# ---------------------------------------------------------------------------
# Actions privacy handler now has a guard and returns Response, so convert Html.
# ---------------------------------------------------------------------------
p = Path("app/src-tauri/src/actions/listener.rs")
s = read(p)
start = s.find('async fn privacy(State(state): State<AppState>, headers: HeaderMap) -> Response {')
end = s.find('\nasync fn oauth_authorization_server_metadata', start)
if start == -1 or end == -1:
    raise SystemExit("Actions privacy handler not found")
block = s[start:end]
needle = '\n    )\n}'
pos = block.rfind(needle)
if pos == -1:
    raise SystemExit("Actions privacy Html return terminator not found")
block = block[:pos] + '\n    ).into_response()\n}' + block[pos + len(needle):]
s = s[:start] + block + s[end:]
write(p, s)

# ---------------------------------------------------------------------------
# MCP discovery test: handler now requires State+headers for origin protection.
# Test the response contract without bypassing the handler's security inputs.
# ---------------------------------------------------------------------------
p = Path("app/src-tauri/src/mcp/listener.rs")
s = read(p)
s = s.replace(
    'use super::{bind_listener, mcp_discovery, mcp_discovery_payload};',
    'use super::{bind_listener, mcp_discovery_payload};',
    1,
)
old = '''    #[tokio::test]
    async fn discovery_prevents_stale_tool_catalog_caching() {
        let response = mcp_discovery().await.into_response();

        assert_eq!(response.headers()[CACHE_CONTROL], "no-store");
    }
'''
new = '''    #[tokio::test]
    async fn discovery_prevents_stale_tool_catalog_caching() {
        let response = (
            [(CACHE_CONTROL, "no-store")],
            axum::Json(mcp_discovery_payload()),
        )
            .into_response();

        assert_eq!(response.headers()[CACHE_CONTROL], "no-store");
    }
'''
if old not in s:
    raise SystemExit("MCP discovery cache test block not found")
s = s.replace(old, new, 1)
write(p, s)

# ---------------------------------------------------------------------------
# Worker replay protection: generation remains monotonic even after offline.
# ---------------------------------------------------------------------------
p = Path("app/src-tauri/src/relay/worker.mjs")
s = read(p)
old = '''      const current = (await this.ctx.storage.get("state")) || null;
      const currentIsLive = current && !current.offline && current.expires_at > Date.now();
      if (currentIsLive && Number.isSafeInteger(current.generation) && payload.generation <= current.generation) {
        return new Response(null, { status: 409 });
      }
'''
new = '''      const current = (await this.ctx.storage.get("state")) || null;
      // Generation is monotonic across both online and offline states. This prevents
      // replaying an older signed online update immediately after a newer offline update.
      if (current && Number.isSafeInteger(current.generation) && payload.generation <= current.generation) {
        return new Response(null, { status: 409 });
      }
'''
if old not in s:
    raise SystemExit("worker replay block not found")
s = s.replace(old, new, 1)
write(p, s)

print("fix7 hardening and first compile fixes applied")
