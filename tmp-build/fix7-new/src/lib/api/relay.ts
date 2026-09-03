import { invoke } from "@tauri-apps/api/core";

export type RelayService = "mcp" | "actions";

export interface RelayDeploymentResult {
  publicUrl: string;
  workerName: string;
  accountId: string;
  deploymentVersion: number;
  message: string;
}

export interface RelayRemoteStatus {
  configured: boolean;
  publicUrl: string;
  state: string;
  backend: string;
  expiresIn: number;
  detail: string;
}

export async function deployCloudflareRelay(
  id: string,
  service: RelayService,
  accountId: string,
  apiToken: string,
): Promise<RelayDeploymentResult> {
  return invoke<RelayDeploymentResult>("deploy_cloudflare_relay", {
    id,
    service,
    accountId,
    apiToken,
  });
}

export async function getCloudflareRelayStatus(
  id: string,
  service: RelayService,
): Promise<RelayRemoteStatus> {
  return invoke<RelayRemoteStatus>("get_cloudflare_relay_status", { id, service });
}

export async function syncCloudflareRelay(
  id: string,
  service: RelayService,
): Promise<RelayRemoteStatus> {
  return invoke<RelayRemoteStatus>("sync_cloudflare_relay", { id, service });
}

export async function deleteCloudflareRelay(
  id: string,
  service: RelayService,
  accountId: string,
  apiToken: string,
): Promise<void> {
  return invoke("delete_cloudflare_relay", {
    id,
    service,
    accountId,
    apiToken,
  });
}
