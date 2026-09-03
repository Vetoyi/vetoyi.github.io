const RELAY_VERSION = "1";
const MAX_ADMIN_BODY_BYTES = 4096;
const MAX_CLOCK_SKEW_MS = 5 * 60 * 1000;
const MIN_LEASE_SECONDS = 30;
const MAX_LEASE_SECONDS = 15 * 60;
const STATE_KEY = "relay-state";

function jsonResponse(body, status = 200, extraHeaders = {}) {
  const headers = new Headers(extraHeaders);
  headers.set("content-type", "application/json; charset=utf-8");
  headers.set("cache-control", "no-store");
  headers.set("x-content-type-options", "nosniff");
  return new Response(JSON.stringify(body), { status, headers });
}

function base64UrlToBytes(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new Error("invalid base64url");
  }
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((value.length + 3) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function canonicalUpdate(payload) {
  return [
    "ctm-relay-v1",
    `service=${payload.service}`,
    `target=${payload.offline ? "" : payload.target}`,
    `offline=${payload.offline ? "1" : "0"}`,
    `generation=${payload.generation}`,
    `timestamp_ms=${payload.timestamp_ms}`,
    `lease_seconds=${payload.lease_seconds}`,
    `nonce=${payload.nonce}`,
  ].join("\n");
}

function validNonce(nonce) {
  return typeof nonce === "string" && /^[A-Za-z0-9_-]{16,96}$/.test(nonce);
}

function normalizeQuickTarget(raw) {
  if (typeof raw !== "string" || raw.length > 2048) return null;
  let url;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.username || url.password || url.hash) return null;
  if (url.port && url.port !== "443") return null;
  const host = url.hostname.toLowerCase();
  if (!host.endsWith(".trycloudflare.com") || host === "trycloudflare.com") return null;
  if (!/^[a-z0-9.-]+$/.test(host)) return null;
  if (url.pathname !== "/" || url.search) return null;
  return `https://${host}`;
}

async function importAdminPublicKey(env) {
  const raw = base64UrlToBytes(env.ADMIN_PUBLIC_KEY_B64);
  if (raw.byteLength !== 32) throw new Error("invalid public key length");
  return crypto.subtle.importKey("raw", raw, { name: "Ed25519" }, false, ["verify"]);
}

async function verifyAdminUpdate(request, env, payload) {
  const signatureHeader = request.headers.get("x-ctm-signature") || "";
  let signature;
  try {
    signature = base64UrlToBytes(signatureHeader);
  } catch {
    return false;
  }
  if (signature.byteLength !== 64) return false;
  const key = await importAdminPublicKey(env);
  const message = new TextEncoder().encode(canonicalUpdate(payload));
  return crypto.subtle.verify({ name: "Ed25519" }, key, signature, message);
}

function pathAllowed(service, pathname) {
  if (service === "mcp") {
    return new Set([
      "/mcp",
      "/.well-known/oauth-authorization-server",
      "/.well-known/oauth-protected-resource",
      "/oauth/authorize",
      "/oauth/token",
    ]).has(pathname);
  }
  if (service === "actions") {
    if (/^\/actions\/[A-Za-z0-9_.-]{1,128}$/.test(pathname)) return true;
    return new Set([
      "/health",
      "/openapi.json",
      "/privacy",
      "/.well-known/oauth-authorization-server",
      "/oauth/authorize",
      "/oauth/token",
    ]).has(pathname);
  }
  return false;
}

function methodAllowed(method) {
  return new Set(["GET", "POST", "DELETE", "OPTIONS", "HEAD"]).has(method);
}

async function getStateStub(env) {
  const id = env.RELAY_STATE.idFromName(STATE_KEY);
  return env.RELAY_STATE.get(id);
}

async function resolveBackend(env) {
  const stub = await getStateStub(env);
  const response = await stub.fetch("https://relay-state.internal/resolve", { method: "GET" });
  if (!response.ok) return { online: false };
  return response.json();
}

async function handlePublicHealth(env) {
  const state = await resolveBackend(env);
  return jsonResponse({
    ok: true,
    relayVersion: RELAY_VERSION,
    service: env.SERVICE_KIND,
    backend: state.online ? "online" : "offline",
    expiresIn: state.online ? state.expires_in : 0,
  });
}

async function handleAdminUpdate(request, env) {
  if (request.method !== "POST") return new Response(null, { status: 404 });
  const contentLength = Number(request.headers.get("content-length") || "0");
  if (Number.isFinite(contentLength) && contentLength > MAX_ADMIN_BODY_BYTES) {
    return new Response(null, { status: 413 });
  }
  const text = await request.text();
  if (new TextEncoder().encode(text).byteLength > MAX_ADMIN_BODY_BYTES) {
    return new Response(null, { status: 413 });
  }
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    return new Response(null, { status: 400 });
  }

  if (!payload || payload.service !== env.SERVICE_KIND) return new Response(null, { status: 403 });
  if (!Number.isSafeInteger(payload.generation) || payload.generation <= 0) return new Response(null, { status: 400 });
  if (!Number.isSafeInteger(payload.timestamp_ms)) return new Response(null, { status: 400 });
  if (!Number.isSafeInteger(payload.lease_seconds) || payload.lease_seconds < MIN_LEASE_SECONDS || payload.lease_seconds > MAX_LEASE_SECONDS) {
    return new Response(null, { status: 400 });
  }
  if (!validNonce(payload.nonce) || typeof payload.offline !== "boolean") return new Response(null, { status: 400 });
  if (Math.abs(Date.now() - payload.timestamp_ms) > MAX_CLOCK_SKEW_MS) return new Response(null, { status: 409 });

  if (payload.offline) {
    payload.target = "";
  } else {
    const normalized = normalizeQuickTarget(payload.target);
    if (!normalized) return new Response(null, { status: 400 });
    payload.target = normalized;
  }

  let verified = false;
  try {
    verified = await verifyAdminUpdate(request, env, payload);
  } catch {
    verified = false;
  }
  if (!verified) return new Response(null, { status: 403 });

  const stub = await getStateStub(env);
  const internal = new Request("https://relay-state.internal/update", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const response = await stub.fetch(internal);
  if (!response.ok) return new Response(null, { status: response.status });
  return jsonResponse({ ok: true, service: env.SERVICE_KIND, relayVersion: RELAY_VERSION });
}

async function proxyRequest(request, env) {
  if (!methodAllowed(request.method)) return new Response(null, { status: 405 });
  const incoming = new URL(request.url);
  if (!pathAllowed(env.SERVICE_KIND, incoming.pathname)) return new Response(null, { status: 404 });

  const state = await resolveBackend(env);
  if (!state.online || typeof state.target !== "string") {
    return jsonResponse({ error: "backend_temporarily_offline" }, 503, { "retry-after": "30" });
  }

  const target = new URL(state.target);
  target.pathname = incoming.pathname;
  target.search = incoming.search;

  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("cf-connecting-ip");
  headers.delete("cf-ipcountry");
  headers.delete("cf-ray");
  headers.delete("cf-visitor");
  headers.set("x-forwarded-proto", "https");
  headers.set("x-forwarded-host", incoming.host);
  headers.set("x-ctm-relay", "1");
  headers.set("x-ctm-origin-auth", env.ORIGIN_AUTH_TOKEN);

  const init = {
    method: request.method,
    headers,
    redirect: "manual",
  };
  if (request.method !== "GET" && request.method !== "HEAD") init.body = request.body;

  try {
    const upstream = await fetch(new Request(target.toString(), init));
    const responseHeaders = new Headers(upstream.headers);
    responseHeaders.set("x-ctm-relay", RELAY_VERSION);
    responseHeaders.set("cache-control", responseHeaders.get("cache-control") || "no-store");
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    });
  } catch {
    return jsonResponse({ error: "backend_unreachable" }, 502, { "retry-after": "15" });
  }
}

export class RelayState {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.hostname !== "relay-state.internal") return new Response(null, { status: 404 });

    if (url.pathname === "/update" && request.method === "POST") {
      const payload = await request.json();
      const current = (await this.ctx.storage.get("state")) || null;
      const currentIsLive = current && !current.offline && current.expires_at > Date.now();
      if (currentIsLive && Number.isSafeInteger(current.generation) && payload.generation <= current.generation) {
        return new Response(null, { status: 409 });
      }
      if (payload.offline) {
        await this.ctx.storage.put("state", {
          generation: payload.generation,
          offline: true,
          expires_at: 0,
        });
      } else {
        await this.ctx.storage.put("state", {
          generation: payload.generation,
          target: payload.target,
          offline: false,
          expires_at: Date.now() + payload.lease_seconds * 1000,
        });
      }
      return jsonResponse({ ok: true });
    }

    if (url.pathname === "/resolve" && request.method === "GET") {
      const current = (await this.ctx.storage.get("state")) || null;
      if (!current || current.offline || !current.target || !current.expires_at || current.expires_at <= Date.now()) {
        return jsonResponse({ online: false });
      }
      return jsonResponse({
        online: true,
        target: current.target,
        expires_in: Math.max(0, Math.floor((current.expires_at - Date.now()) / 1000)),
      });
    }

    return new Response(null, { status: 404 });
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/__ctm/health") return handlePublicHealth(env);
    if (url.pathname === "/__ctm/update") return handleAdminUpdate(request, env);
    if (url.pathname.startsWith("/__ctm/")) return new Response(null, { status: 404 });
    return proxyRequest(request, env);
  },
};
