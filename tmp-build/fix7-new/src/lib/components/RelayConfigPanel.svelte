<script lang="ts">
  import { onMount } from "svelte";
  import { confirm } from "@tauri-apps/plugin-dialog";
  import {
    deleteCloudflareRelay,
    deployCloudflareRelay,
    getCloudflareRelayStatus,
    syncCloudflareRelay,
    type RelayRemoteStatus,
    type RelayService,
  } from "$lib/api/relay";
  import { showToast } from "$lib/stores/toast";
  import type { RelayConfig } from "$lib/types";

  interface Props {
    workspaceId: string;
    service: RelayService;
    relay?: RelayConfig;
    onChanged?: () => void | Promise<void>;
  }

  let { workspaceId, service, relay, onChanged }: Props = $props();

  const emptyRelay: RelayConfig = {
    enabled: false,
    account_id: "",
    worker_name: "",
    public_url: "",
    deployment_version: 0,
  };

  let accountId = $state("");
  let apiToken = $state("");
  let busy = $state<"" | "deploy" | "status" | "sync" | "delete">("");
  let remote = $state<RelayRemoteStatus | null>(null);

  const current = $derived(relay ?? emptyRelay);
  const configured = $derived(Boolean(current.enabled && current.public_url));

  $effect(() => {
    if (!accountId || accountId === current.account_id) {
      accountId = current.account_id ?? "";
    }
  });

  onMount(() => {
    if (configured) void refreshStatus();
  });

  async function changed() {
    if (onChanged) await onChanged();
  }

  async function deploy() {
    if (busy) return;
    const account = accountId.trim();
    const token = apiToken.trim();
    if (!/^[0-9a-fA-F]{32}$/.test(account)) {
      showToast("Account ID 应为 32 位十六进制字符串。", {
        title: "Account ID 无效",
        kind: "error",
      });
      return;
    }
    if (token.length < 20) {
      showToast("请粘贴刚创建的 Cloudflare Account API Token。", {
        title: "缺少 API Token",
        kind: "error",
      });
      return;
    }

    busy = "deploy";
    try {
      const result = await deployCloudflareRelay(workspaceId, service, account, token);
      apiToken = "";
      accountId = result.accountId;
      showToast(`${result.message}\n${result.publicUrl}`, {
        title: configured ? "Relay 已更新" : "Relay 部署成功",
        kind: "success",
        duration: 9000,
      });
      await changed();
      await refreshStatus();
    } catch (error) {
      apiToken = "";
      showToast(String(error), { title: "Relay 部署失败", kind: "error", duration: 12000 });
    } finally {
      busy = "";
    }
  }

  async function refreshStatus() {
    if (busy && busy !== "status") return;
    const ownsBusy = !busy;
    if (ownsBusy) busy = "status";
    try {
      remote = await getCloudflareRelayStatus(workspaceId, service);
    } catch (error) {
      remote = {
        configured: true,
        publicUrl: current.public_url,
        state: "error",
        backend: "unknown",
        expiresIn: 0,
        detail: String(error),
      };
    } finally {
      if (ownsBusy) busy = "";
    }
  }

  async function syncNow() {
    if (busy) return;
    busy = "sync";
    try {
      remote = await syncCloudflareRelay(workspaceId, service);
      showToast("Relay 已与当前 Quick Tunnel 重新同步，并已启动自动续租。", {
        title: "同步成功",
        kind: "success",
      });
    } catch (error) {
      showToast(String(error), { title: "同步失败", kind: "error", duration: 9000 });
    } finally {
      busy = "";
    }
  }

  async function removeRelay() {
    if (busy || !configured) return;
    const account = accountId.trim() || current.account_id;
    const token = apiToken.trim();
    if (!token) {
      showToast("删除 Cloudflare Worker 需要再次输入部署用的 Account API Token；程序不会保存该 Token。", {
        title: "需要 API Token",
        kind: "warning",
        duration: 8000,
      });
      return;
    }
    const ok = await confirm(
      `确定删除 ${service.toUpperCase()} 的 Quick Tunnel Relay？\n\n稳定 Relay URL 将立即失效，已经配置该 URL 的 ChatGPT 应用也会停止工作。`,
      {
        title: "删除 Quick Tunnel Relay",
        kind: "warning",
        okLabel: "删除",
        cancelLabel: "取消",
      },
    );
    if (!ok) return;

    busy = "delete";
    try {
      await deleteCloudflareRelay(workspaceId, service, account, token);
      apiToken = "";
      remote = null;
      showToast("Relay Worker 已删除，本机 Relay 配置和设备凭据已清理。", {
        title: "Relay 已删除",
        kind: "success",
      });
      await changed();
    } catch (error) {
      apiToken = "";
      showToast(String(error), { title: "删除 Relay 失败", kind: "error", duration: 10000 });
    } finally {
      busy = "";
    }
  }

  async function copyUrl() {
    if (!current.public_url) return;
    try {
      await navigator.clipboard.writeText(
        service === "mcp" ? `${current.public_url.replace(/\/$/, "")}/mcp` : current.public_url,
      );
      showToast(service === "mcp" ? "稳定 MCP endpoint 已复制。" : "稳定 Relay 根地址已复制。", {
        kind: "success",
      });
    } catch (error) {
      showToast(String(error), { title: "复制失败", kind: "error" });
    }
  }
</script>

<section class="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
  <div class="flex flex-wrap items-start justify-between gap-3">
    <div>
      <p class="text-sm font-semibold">Quick Tunnel Relay <span class="text-xs font-normal text-[var(--color-text-muted)]">实验性</span></p>
      <p class="mt-1 max-w-3xl text-xs leading-5 text-[var(--color-text-muted)]">
        用稳定的 Cloudflare Workers 地址代理当前随机 trycloudflare.com 地址。Quick Tunnel 仍负责把本机暴露到公网；每次开机得到新 URL 后，程序会自动签名同步并续租，ChatGPT 只需保存 Relay 地址。
      </p>
    </div>
    {#if configured}
      <span class="rounded-md bg-[var(--color-success)]/10 px-2 py-1 text-xs font-medium text-[var(--color-success)]">已部署</span>
    {:else}
      <span class="rounded-md bg-[var(--color-text-muted)]/10 px-2 py-1 text-xs text-[var(--color-text-muted)]">未部署</span>
    {/if}
  </div>

  {#if configured}
    <div class="mt-3 grid gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] p-3 text-xs">
      <div class="grid gap-1">
        <span class="text-[var(--color-text-muted)]">稳定公网地址</span>
        <div class="flex min-w-0 items-center gap-2">
          <code class="min-w-0 flex-1 break-all text-[var(--color-text-secondary)]">
            {service === "mcp" ? `${current.public_url.replace(/\/$/, "")}/mcp` : current.public_url}
          </code>
          <button type="button" class="tx-btn-ghost shrink-0 px-2 py-1 text-xs" onclick={() => void copyUrl()}>复制</button>
        </div>
      </div>
      <p class="text-[var(--color-text-muted)]">Worker：{current.worker_name} · 部署协议 v{current.deployment_version || 1}</p>
      {#if remote}
        <p class:health-good={remote.state === "reachable" && remote.backend === "online"} class="text-[var(--color-text-muted)]">
          {remote.detail}
        </p>
      {/if}
    </div>
  {/if}

  <div class="mt-3 grid gap-3 md:grid-cols-2">
    <label class="grid gap-1">
      <span class="text-xs text-[var(--color-text-muted)]">Cloudflare Account ID</span>
      <input
        type="text"
        class="rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 font-mono text-sm"
        maxlength="32"
        placeholder="32 位 Account ID"
        bind:value={accountId}
      />
      <span class="text-[11px] text-[var(--color-text-muted)]">Account ID 不是密码；部署成功后会随工作区保存。</span>
    </label>

    <label class="grid gap-1">
      <span class="text-xs text-[var(--color-text-muted)]">Account API Token</span>
      <input
        type="password"
        class="rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-2.5 py-1.5 font-mono text-sm"
        autocomplete="off"
        spellcheck="false"
        placeholder="Workers Scripts → Edit / Write"
        bind:value={apiToken}
      />
      <span class="text-[11px] text-[var(--color-text-muted)]">仅用于本次部署/升级/删除请求；不会写入工作区，也不会长期保存。部署完成后输入框会立即清空。</span>
    </label>
  </div>

  <div class="mt-3 rounded-md border border-[var(--color-border)] px-3 py-2 text-[11px] leading-5 text-[var(--color-text-muted)]">
    Relay 的日常 target 更新不使用 Cloudflare API Token，而使用本机生成的 Ed25519 设备密钥；Windows 安装包把私钥与 Origin 凭据保存在 Windows Credential Manager。Worker 只接受合法的 <code>https://*.trycloudflare.com</code> 根地址，且不会在公开健康接口中泄露当前随机 URL。
  </div>

  <div class="mt-3 flex flex-wrap justify-end gap-2">
    {#if configured}
      <button type="button" class="tx-btn-ghost px-3 py-1.5 text-sm disabled:opacity-50" disabled={Boolean(busy)} onclick={() => void refreshStatus()}>
        {busy === "status" ? "刷新中…" : "刷新状态"}
      </button>
      <button type="button" class="tx-btn-ghost px-3 py-1.5 text-sm disabled:opacity-50" disabled={Boolean(busy)} onclick={() => void syncNow()}>
        {busy === "sync" ? "同步中…" : "重新同步 Relay"}
      </button>
      <button type="button" class="tx-btn-ghost px-3 py-1.5 text-sm text-[var(--danger)] disabled:opacity-50" disabled={Boolean(busy)} onclick={() => void removeRelay()}>
        {busy === "delete" ? "删除中…" : "删除 Relay"}
      </button>
    {/if}
    <button
      type="button"
      class="rounded-md bg-[var(--color-accent)] px-3 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
      disabled={Boolean(busy)}
      onclick={() => void deploy()}
    >
      {busy === "deploy" ? "部署中…" : configured ? "升级 / 重新部署" : "部署 Relay"}
    </button>
  </div>
</section>
