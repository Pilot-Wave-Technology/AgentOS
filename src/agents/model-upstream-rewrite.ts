import type { Api, Model } from "@mariozechner/pi-ai";
import type { OpenClawConfig } from "../config/config.js";

/**
 * Look up the `upstreamId` configured for a given (provider, modelId) pair
 * in `cfg.models.providers`. Used by synthetic context-window variants
 * (e.g. `google-pro-100k/gemini-2.5-pro-100k`) that masquerade as the real
 * upstream model at API-call time.
 */
export function findUpstreamIdInConfig(params: {
  cfg?: OpenClawConfig;
  provider: string;
  modelId: string;
}): string | undefined {
  const providerEntry = params.cfg?.models?.providers?.[params.provider];
  if (!providerEntry || !Array.isArray(providerEntry.models)) {
    return undefined;
  }
  const match = providerEntry.models.find((m) => m?.id === params.modelId);
  const upstreamId = match?.upstreamId;
  if (typeof upstreamId === "string" && upstreamId.trim().length > 0) {
    return upstreamId;
  }
  return undefined;
}

/**
 * Produce a copy of `model` with `id` swapped to the upstream value when
 * a synthetic variant is configured for this (provider, modelId).
 *
 * Applied at the createAgentSession boundary so pi-coding-agent makes the
 * actual upstream API call with the real model name, while the rest of the
 * gateway (lookupContextTokens, logs, model registry) continues to see the
 * synthetic id. Returns the model unchanged when no upstream rewrite is
 * configured.
 */
export function applyUpstreamIdForApiCall(params: {
  cfg?: OpenClawConfig;
  provider: string;
  modelId: string;
  model: Model<Api>;
}): Model<Api> {
  const upstreamId = findUpstreamIdInConfig({
    cfg: params.cfg,
    provider: params.provider,
    modelId: params.modelId,
  });
  if (!upstreamId || upstreamId === params.model.id) {
    return params.model;
  }
  return { ...params.model, id: upstreamId };
}
