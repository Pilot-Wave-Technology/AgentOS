import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProviderConfig } from "../../../src/agents/models-config.providers.js";
import { clearCache, getCachedProviders, refreshCache } from "./cache.js";

type FetchStub = ReturnType<typeof vi.fn>;

function mockJsonResponse(
  body: unknown,
  init: { status?: number } = {},
): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "content-type": "application/json" },
  });
}

function mockRawResponse(text: string, init: { status?: number } = {}): Response {
  return new Response(text, {
    status: init.status ?? 200,
    headers: { "content-type": "text/plain" },
  });
}

const acmeProvider: ProviderConfig = {
  baseUrl: "https://acme.example",
  apiKey: "ACME_KEY",
  models: [],
};

const betaProvider: ProviderConfig = {
  baseUrl: "https://beta.example",
  apiKey: "BETA_KEY",
  models: [{ id: "beta-model" }],
};

describe("cache", () => {
  let fetchSpy: FetchStub;

  beforeEach(() => {
    fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    clearCache();
  });

  it("populates the cache with the parsed providers map on HTTP 200", async () => {
    fetchSpy.mockResolvedValueOnce(mockJsonResponse({ providers: { acme: acmeProvider } }));

    await refreshCache("https://control.example", "tok");

    expect(getCachedProviders()).toEqual({ acme: acmeProvider });
  });

  it("returns a shallow clone from getCachedProviders (mutations do not leak)", async () => {
    fetchSpy.mockResolvedValueOnce(
      mockJsonResponse({ providers: { acme: acmeProvider, beta: betaProvider } }),
    );

    await refreshCache("https://control.example", "tok");

    const first = getCachedProviders();
    // Mutate the returned object; a subsequent call must NOT see the mutation.
    delete first.acme;
    (first as Record<string, ProviderConfig>).injected = acmeProvider;

    const second = getCachedProviders();
    expect(second).toEqual({ acme: acmeProvider, beta: betaProvider });
    expect(second).not.toHaveProperty("injected");
  });

  it("throws with HTTP status in the message on non-2xx and leaves cache untouched", async () => {
    // Seed the cache with a known-good state first.
    fetchSpy.mockResolvedValueOnce(mockJsonResponse({ providers: { acme: acmeProvider } }));
    await refreshCache("https://control.example", "tok");
    const before = getCachedProviders();

    fetchSpy.mockResolvedValueOnce(mockJsonResponse({}, { status: 500 }));
    await expect(refreshCache("https://control.example", "tok")).rejects.toThrow(/HTTP 500/);

    expect(getCachedProviders()).toEqual(before);
  });

  it("throws and leaves the cache untouched on non-JSON body", async () => {
    fetchSpy.mockResolvedValueOnce(mockJsonResponse({ providers: { acme: acmeProvider } }));
    await refreshCache("https://control.example", "tok");
    const before = getCachedProviders();

    fetchSpy.mockResolvedValueOnce(mockRawResponse("<html>oops</html>"));
    await expect(refreshCache("https://control.example", "tok")).rejects.toBeInstanceOf(Error);

    expect(getCachedProviders()).toEqual(before);
  });

  it("throws with a clear message when the response is missing top-level `providers`", async () => {
    fetchSpy.mockResolvedValueOnce(mockJsonResponse({ providers: { acme: acmeProvider } }));
    await refreshCache("https://control.example", "tok");
    const before = getCachedProviders();

    fetchSpy.mockResolvedValueOnce(mockJsonResponse({ notProviders: {} }));
    await expect(refreshCache("https://control.example", "tok")).rejects.toThrow(
      /top-level `providers`/,
    );

    expect(getCachedProviders()).toEqual(before);
  });

  it("throws with a clear message when the root is not a JSON object", async () => {
    fetchSpy.mockResolvedValueOnce(mockJsonResponse(["not", "an", "object"]));
    await expect(refreshCache("https://control.example", "tok")).rejects.toThrow(
      /not a JSON object/,
    );

    expect(getCachedProviders()).toEqual({});
  });

  it("normalises trailing slash on the URL argument to a single separator", async () => {
    fetchSpy.mockResolvedValueOnce(mockJsonResponse({ providers: {} }));

    await refreshCache("https://control.example/", "tok");

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy.mock.calls[0][0]).toBe("https://control.example/internal/model_catalog");
  });

  it("passes the bearer token in the Authorization header", async () => {
    fetchSpy.mockResolvedValueOnce(mockJsonResponse({ providers: {} }));

    await refreshCache("https://control.example", "sekret-token");

    const init = fetchSpy.mock.calls[0][1] as RequestInit | undefined;
    const headers = init?.headers as Record<string, string> | undefined;
    expect(headers).toEqual({ Authorization: "Bearer sekret-token" });
  });

  it("clearCache() empties a populated cache", async () => {
    fetchSpy.mockResolvedValueOnce(mockJsonResponse({ providers: { acme: acmeProvider } }));
    await refreshCache("https://control.example", "tok");
    expect(getCachedProviders()).toEqual({ acme: acmeProvider });

    clearCache();

    expect(getCachedProviders()).toEqual({});
  });
});
