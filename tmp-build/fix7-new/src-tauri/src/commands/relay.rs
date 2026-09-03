use tauri::State;
use zeroize::Zeroize;

use crate::app_state::AppState;
use crate::error::{AppError, AppResult};
use crate::relay::{self, RelayDeploymentResult, RelayRemoteStatus};
use crate::runtime::ServiceKind;
use crate::tunnel::{supervisor, TunnelServiceKind};
use crate::workspace::{RelayConfig, WorkspaceProfile};

use super::runtime::{restart_actions_by_id, restart_mcp_by_id};

fn profile_by_id(state: &AppState, id: &str) -> AppResult<WorkspaceProfile> {
    state.with_workspaces(|store| {
        store
            .get(id)
            .cloned()
            .ok_or_else(|| AppError::Message(format!("workspace not found: {id}")))
    })
}

fn parse_kind(service: &str) -> AppResult<TunnelServiceKind> {
    TunnelServiceKind::parse(service)
}

fn set_relay_config(
    state: &AppState,
    id: &str,
    kind: TunnelServiceKind,
    config: RelayConfig,
) -> AppResult<WorkspaceProfile> {
    state.with_workspaces(|store| {
        let mut profile = store
            .get(id)
            .cloned()
            .ok_or_else(|| AppError::Message(format!("workspace not found: {id}")))?;
        match kind {
            TunnelServiceKind::Mcp => profile.tunnel.relay = config,
            TunnelServiceKind::Actions => profile.actions.relay = config,
        }
        store.update(profile.clone())?;
        Ok(profile)
    })
}

async fn restart_if_running(
    state: &AppState,
    id: &str,
    kind: TunnelServiceKind,
) -> AppResult<()> {
    let running = state.with_runtime(|runtime| {
        Ok(runtime.is_running(
            id,
            match kind {
                TunnelServiceKind::Mcp => ServiceKind::Mcp,
                TunnelServiceKind::Actions => ServiceKind::Actions,
            },
        ))
    })?;
    if !running {
        return Ok(());
    }
    match kind {
        TunnelServiceKind::Mcp => {
            let _ = restart_mcp_by_id(state, id).await?;
        }
        TunnelServiceKind::Actions => {
            let _ = restart_actions_by_id(state, id).await?;
        }
    }
    Ok(())
}

#[tauri::command]
pub async fn deploy_cloudflare_relay(
    state: State<'_, AppState>,
    id: String,
    service: String,
    account_id: String,
    mut api_token: String,
) -> AppResult<RelayDeploymentResult> {
    let kind = parse_kind(&service)?;
    let profile = profile_by_id(&state, &id)?;
    let previous_canonical = match kind {
        TunnelServiceKind::Mcp => profile.mcp_canonical_public_url(),
        TunnelServiceKind::Actions => profile.actions_canonical_public_url(),
    };
    let deploy_result = relay::deploy(&profile, kind, &account_id, &api_token).await;
    api_token.zeroize();
    let mut result = deploy_result?;

    let config = RelayConfig {
        enabled: true,
        account_id: result.account_id.clone(),
        worker_name: result.worker_name.clone(),
        public_url: result.public_url.clone(),
        deployment_version: result.deployment_version,
    };
    let updated = set_relay_config(&state, &id, kind, config)?;
    let next_canonical = match kind {
        TunnelServiceKind::Mcp => updated.mcp_canonical_public_url(),
        TunnelServiceKind::Actions => updated.actions_canonical_public_url(),
    };

    // Only restart when the application identity actually changes. A normal Worker code
    // upgrade keeps the same workers.dev URL and therefore must not churn the Quick Tunnel
    // or consume another account-less Tunnel allocation.
    if previous_canonical != next_canonical {
        if let Err(error) = restart_if_running(&state, &id, kind).await {
            return Err(AppError::Message(format!(
                "Relay 已部署到 {}，但当前服务自动重启失败：{error}。Relay 配置已保存；请手动重新启动该服务一次。",
                result.public_url
            )));
        }
    }

    // If the tunnel is managed independently from the local runtime, synchronize it too.
    let settings = state.with_settings(|store| Ok(store.settings()))?;
    let status = {
        let guard = supervisor().lock().await;
        guard.status(&updated, kind, &settings)
    };
    if status.state == "running" && relay::is_valid_quick_target(&status.public_url) {
        let current = profile_by_id(&state, &id)?;
        if let Err(error) = relay::activate_lease(&current, kind, &status.public_url).await {
            result.message.push_str(&format!(
                " 当前 Quick Tunnel 已运行，但 Relay 首次 target 同步失败：{error}。后台续租任务会自动重试，无需重启服务。"
            ));
        }
    }

    Ok(result)
}

#[tauri::command]
pub async fn get_cloudflare_relay_status(
    state: State<'_, AppState>,
    id: String,
    service: String,
) -> AppResult<RelayRemoteStatus> {
    let kind = parse_kind(&service)?;
    let profile = profile_by_id(&state, &id)?;
    Ok(relay::remote_status(&profile, kind).await)
}

#[tauri::command]
pub async fn sync_cloudflare_relay(
    state: State<'_, AppState>,
    id: String,
    service: String,
) -> AppResult<RelayRemoteStatus> {
    let kind = parse_kind(&service)?;
    let profile = profile_by_id(&state, &id)?;
    if !relay::relay_enabled(&profile, kind) {
        return Err(AppError::Message("该服务尚未启用 Quick Tunnel Relay。".into()));
    }
    let settings = state.with_settings(|store| Ok(store.settings()))?;
    let status = {
        let guard = supervisor().lock().await;
        guard.status(&profile, kind, &settings)
    };
    if status.state != "running" || !relay::is_valid_quick_target(&status.public_url) {
        return Err(AppError::Message(
            "当前没有正在运行的 Cloudflare Quick Tunnel，无法同步 Relay target。".into(),
        ));
    }
    relay::activate_lease(&profile, kind, &status.public_url).await?;
    Ok(relay::remote_status(&profile, kind).await)
}

#[tauri::command]
pub async fn delete_cloudflare_relay(
    state: State<'_, AppState>,
    id: String,
    service: String,
    account_id: String,
    mut api_token: String,
) -> AppResult<()> {
    let kind = parse_kind(&service)?;
    let profile = profile_by_id(&state, &id)?;
    relay::deactivate_lease_task(&profile.id, kind).await;
    let delete_result = relay::delete_remote(&profile, kind, &account_id, &api_token).await;
    api_token.zeroize();
    delete_result?;
    let _ = set_relay_config(&state, &id, kind, RelayConfig::default())?;
    restart_if_running(&state, &id, kind).await?;
    Ok(())
}
