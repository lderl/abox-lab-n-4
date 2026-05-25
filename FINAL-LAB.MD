# Vin's Questions — AI Infrastructure Research


## 1. How could we handle "agent got stuck" scenarios?

Our stack has several complementary layers for this.

**Gateway-level timeouts (agentgateway).** Every request to an agent passes through `agentgateway-proxy` (port 8082) or `agentgateway-external` (port 80). Gateway API `HTTPRoute` supports `spec.rules[].timeouts.request` and `timeouts.backendRequest`. Setting these to, say, `"30s"` means agentgateway automatically cancels the request and returns `504 Gateway Timeout` to the caller — no agent-side code needed.

```yaml
# Example on any HTTPRoute that fronts an agent
spec:
  rules:
    - timeouts:
        request: "30s"
        backendRequest: "25s"
```

**Observability via Phoenix.** Our `agentgateway-tracing.yaml` sends every span to Phoenix (OTLP gRPC → `phoenix-svc:4317`). A "stuck" agent produces an unusually long open span. Phoenix's trace explorer lets you filter by `duration > threshold`, or you can alert on span anomalies using the Phoenix API at `/v1/spans`.

**Kubernetes liveness/readiness probes.** If an agent pod stops responding, Kubernetes restarts it automatically. kagent-managed agents running as Deployments inherit this behavior.

**A2A protocol timeout.** When agents communicate via JSON-RPC 2.0 A2A (`message/send`), the calling agent controls the HTTP client timeout. In our `a2a_client.py` this is configurable — set `timeout=` on the httpx/requests call to bound the wait.

**kagent-level MCP timeout.** The `RemoteMCPServer` resource (used by kagent to register MCP servers) has its own timeout fields that kick in before the gateway even sees the issue:

```yaml
apiVersion: kagent.dev/v1alpha2
kind: RemoteMCPServer
spec:
  timeout: 30s          # maximum time for a single tool call response
  sseReadTimeout: 5m0s  # timeout for long-lived SSE streams
  terminateOnClose: true # close the MCP session if client disconnects
```

This is a second independent timeout layer — if an MCP server is slow or hangs, kagent closes the connection at the source without waiting for the gateway to time out first.

---

## 2. Any automatic timeout/circuit breaker patterns coming out of this framework?

Yes — primarily from the agentgateway layer.

**Important architectural note:** agentgateway is **not** Envoy-based. According to the authors (Lin Sun & Christian Posta, Solo.io), they explicitly considered Envoy but concluded that supporting MCP and A2A protocols would require a full re-architecture. Instead, agentgateway is written in **Rust from the ground up** — built on the same lessons as ztunnel (Istio Ambient's zero-trust tunnel), purpose-designed for AI-native traffic patterns.

**HTTPRoute timeouts** are part of the Gateway API standard and work out of the box in agentgateway v2.2.1. They apply to every backend including the OpenAI `AgentgatewayBackend`, so if OpenAI is slow, all agents benefit automatically.

**Retry policies.** agentgateway supports `spec.rules[].retries` on HTTPRoute, including `retryOn: "5xx,reset,connect-failure"` and `numRetries`. This handles transient OpenAI 500/529 errors without any agent code change.

**Circuit breaker.** Because agentgateway is a purpose-built Rust proxy (not Envoy), circuit breaking is implemented natively for AI workloads — aware of token streams, MCP session state, and A2A message flows. If a backend (LLM provider or MCP server) repeatedly fails health checks, agentgateway ejects it from the routing pool automatically.

**mcp-governance layer.** Our `MCPGovernancePolicy` (deployed via `mcpg.yaml`, scanning the `kagent` namespace) can enforce call-depth limits — preventing agents from recursively calling tools indefinitely.

---

## 3. How does kgateway handle model failover?

kgateway (the engine behind agentgateway) implements failover through standard Gateway API primitives.

**Multiple `AgentgatewayBackend` resources.** In our `openai-conf.yaml` we have one backend named `openai`. To add failover, create additional backends:

```yaml
# Primary
apiVersion: agentgateway.dev/v1alpha1
kind: AgentgatewayBackend
metadata:
  name: openai-primary
  namespace: agentgateway-system
spec:
  ai:
    provider:
      openai:
        model: "gpt-4o"

# Fallback (e.g. Anthropic or a local vLLM)
apiVersion: agentgateway.dev/v1alpha1
kind: AgentgatewayBackend
metadata:
  name: openai-fallback
  namespace: agentgateway-system
spec:
  ai:
    provider:
      openai:
        model: "gpt-4o-mini"
```

**Weighted `backendRefs` in HTTPRoute.** The gateway can distribute or failover traffic:

```yaml
rules:
  - backendRefs:
    - name: openai-primary
      weight: 100
    - name: openai-fallback
      weight: 0   # only used if primary is unhealthy
```

When agentgateway's health checks detect the primary is down, traffic shifts automatically to the fallback.

---

## 4. Can we automatically switch from OpenAI to Claude to local model?

**Architecturally yes — with one important caveat (see Q5).**

Our current `ModelConfig` (`gpt-config`) points kagent agents to `agentgateway-proxy.agentgateway-system.svc.cluster.local:8082/v1`. This is the OpenAI-compatible endpoint on our proxy. The full failover chain would be:

1. Create three `AgentgatewayBackend` resources: `openai`, `claude-anthropic`, `local-vllm`.
2. Configure HTTPRoute with health-check-aware failover across the three backends.
3. kagent agents don't need to change — they keep calling the same `baseUrl` in `ModelConfig`; the gateway handles routing.

**What this looks like in practice:**

```
kagent agent → agentgateway-proxy:8082 → [OpenAI healthy?] → OpenAI
                                       → [OpenAI down]     → Claude (via Anthropic API)
                                       → [Claude down]     → local vLLM
```

The switch is automatic at the gateway level, transparent to the agent code.

---

## 5. Could we seamlessly handle the response formats from these providers?

**Partially, depending on the provider.**

**OpenAI ↔ vLLM / Ollama: seamless.** vLLM and Ollama both expose an OpenAI-compatible `/v1/chat/completions` API. Our `AgentgatewayBackend` spec works with them without transformation. kagent agents see no difference.

**OpenAI ↔ Anthropic Claude: not seamless by default.** Anthropic's API uses `/v1/messages` with a different request/response schema (e.g., `content` is an array of typed blocks, not a string). agentgateway's AI transformation features can normalize this, but it requires:

- Using agentgateway's built-in Anthropic backend provider type (if supported in v2.2.1).
- Or deploying a translation sidecar (e.g., `litellm-proxy`) as an intermediate backend that normalizes all providers to OpenAI format.

**Our recommendation for multi-provider:** Route through a LiteLLM proxy as an `AgentgatewayBackend`, and let LiteLLM handle format normalization. agentgateway then sees one consistent OpenAI-format endpoint regardless of which model is active.

**Inference Gateway API.** For self-hosted GPU inference, agentgateway v2.2.1 supports the Kubernetes **Inference Gateway API** with `InferencePool`. This allows routing based not just on health, but on intelligent signals:
- GPU and KV-cache utilization
- Prompt criticality
- Work queue depth
- Active LoRA adapters

This means agentgateway can route an agent's LLM call to the specific vLLM node that already has the relevant KV cache warm — directly relevant to reducing latency in multi-step agent chains (see also Q14, Q15).

---

## 6. Can we version the agents built from kagent?

**Yes — Git is the version control layer.**

Our setup is fully GitOps-driven via Flux. Every Agent CRD lives in git:

```
releases/
  lab3/
    agent.yaml      # web-fetch-agent spec
    mcp-server.yaml
    openai-conf.yaml
```

Versioning works at two levels:

**Infrastructure version (kagent itself).** The `kagent.yaml` HelmRelease pins `tag: "0.7.23"`. A version bump is a one-line git change; Flux reconciles it automatically within 1 hour (or on `flux reconcile`).

**Agent spec version.** The `Agent` CRD doesn't have a native `spec.version` field in v1alpha2, but you have two practical options:

- **Git tags**: `git tag agent-web-fetch-v2` creates a point-in-time snapshot. Roll back by reverting the commit.
- **Labels/annotations**: Add `app.kubernetes.io/version: "2.0.0"` to agent metadata. Our `kagent.yaml` HelmRelease already uses `postRenderers` to inject version labels automatically.

Rollback is a `git revert` + push — Flux handles the rest in under 2 minutes.

---

## 7. Any blue/green or canary deployment patterns for agents?

**Yes — composable from existing primitives.**

**Canary via HTTPRoute weights.** Deploy `web-fetch-agent-v2` alongside `web-fetch-agent-v1`, then split traffic:

```yaml
rules:
  - backendRefs:
    - name: kagent-controller   # routes to v1 agent
      port: 8083
      weight: 90
    - name: kagent-controller-v2
      port: 8083
      weight: 10
```

Shift weight gradually (10% → 50% → 100%) while monitoring Phoenix for error rate and latency.

**Blue/green via namespaces.** Deploy a second kagent instance in `kagent-green` namespace alongside `kagent-blue`. Switch the HTTPRoute's `backendRef` from blue to green atomically. Roll back by switching back — the blue environment stays warm.

**Flux-driven progressive delivery.** Flux v2 supports `Kustomization` dependencies — you can gate the green environment rollout on health checks of the blue environment passing.

**agentregistry as traffic source-of-truth.** Our `agentregistry` (deployed via `inventory.yaml`) maintains `AgentCatalog` CRDs. In a mature setup, the registry drives which agent version is "live," decoupling deployment from traffic routing.

---

## 8. What's the fastmcp-python framework?

FastMCP is a high-level Python framework for building **MCP (Model Context Protocol) servers** with minimal boilerplate.

**Core concept:** Expose any Python function as an MCP tool using a decorator:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("my-server")

@mcp.tool()
def fetch_page(url: str) -> str:
    """Fetch a web page and return its text content."""
    import httpx
    return httpx.get(url).text

if __name__ == "__main__":
    mcp.run()
```

FastMCP automatically generates the JSON Schema for tool parameters from Python type hints, handles the MCP protocol handshake, and supports multiple transports (stdio, SSE, HTTP Streamable). In our lab we use it in `mcp_server_traced.py` with Phoenix OTEL instrumentation layered on top via `openinference-instrumentation-mcp`.

The framework is part of the official `mcp` Python package (`pip install mcp[fastmcp]`) maintained by Anthropic.

**Note on `kmcp`.** The kagent ecosystem also ships `kmcp` — a separate CLI tool specifically for **deploying** MCP servers to Kubernetes. These two tools solve different parts of the problem and work together:

| Tool | Role |
|---|---|
| **FastMCP** | Write the MCP server logic (Python code, decorators, tools) |
| **kmcp** | Scaffold, build, and deploy that server to Kubernetes as an `MCPServer` CRD |

```bash
# kmcp workflow (from "AI Agents in Kubernetes", Ch.3)
kmcp new my-weather-service --language python   # generates production-ready project layout
kmcp build --tag ghcr.io/you/my-weather-service:v1.0   # builds container image
kmcp deploy --image ghcr.io/you/my-weather-service:v1.0 --namespace tools
# → creates MCPServer CRD → kagent auto-discovers it → tools available to all agents
```

The entire flow from idea to agents being able to call your custom tools takes under 10 minutes.

---

## 9. Is it the easiest path to MCP?

**Yes — FastMCP + kmcp together give the shortest path to production MCP on Kubernetes.**

| Approach | Write server | Deploy to k8s | Notes |
|---|---|---|---|
| **FastMCP + kmcp** | Low | Low | Full k8s-native lifecycle, auto-discovery by kagent |
| FastMCP only | Low | Manual | Need to write Dockerfile + Deployment + MCPServer YAML manually |
| Raw `mcp` Python SDK | Medium | Manual | More control, more boilerplate |
| TypeScript MCP SDK | Low-Medium | Manual | Well-supported, requires Node.js |
| Go / Rust SDKs | High | Manual | Early-stage community libraries |

For our stack — Python agents on Kubernetes — FastMCP handles the server logic and kmcp handles the k8s deployment lifecycle. kagent then auto-discovers the deployed `MCPServer` resource and makes all tools immediately available to agents — no manual configuration needed.

The one trade-off: FastMCP abstracts away some lower-level MCP features (resources, prompts, sampling). If you need fine-grained control over those primitives, drop to the raw SDK.

---

## 10. About FinOps: how much control can I have?

Quite a lot — our architecture provides control at every layer.

**Visibility layer (Phoenix).** Every LLM call goes through `agentgateway-proxy` and emits an OTLP span to Phoenix with token counts (`llm.token_count.prompt`, `llm.token_count.completion`). Phoenix's UI shows cost-by-project, and the REST API (`/v1/spans`) lets you build custom dashboards or alerts.

**Enforcement layer (agentgateway policies).** `AgentgatewayPolicy` can set token budgets and rate limits on any Gateway or HTTPRoute. These are Kubernetes-native — declared in YAML, version-controlled in git, reconciled by Flux.

**Governance layer (mcp-governance).** Our `MCPGovernancePolicy` scans the `kagent` namespace and can enforce limits on tool call frequency and patterns. Combined with token budgets, this creates a multi-layer cost defense.

**Summary of available levers:**
- Global token rate limit on the `agentgateway-proxy` gateway
- Per-route limits (different agents, different budgets)
- Per-backend limits (limit what any consumer can spend on OpenAI)
- Tool call depth limits via mcp-governance
- Real-time spend monitoring via Phoenix

---

## 11. Token level / per agent level

Our `gpt-config` ModelConfig routes all kagent agents through the same `agentgateway-proxy` endpoint. For per-agent token budgeting:

**Option A: Separate HTTPRoutes per agent class.** Give `web-fetch-agent` its own HTTPRoute (e.g., `/v1/agents/web-fetch/...`) with a dedicated `AgentgatewayPolicy` targeting that route.

**Option B: Header-based routing.** kagent can inject a custom header per agent (e.g., `X-Agent-Name: web-fetch-agent`). agentgateway's header-matching rules direct to different backends/policies without multiple routes.

**Option C: Separate `agentgateway-proxy` gateways.** Deploy a `Gateway` per team or agent class, each with its own token quota. Agents in different namespaces point to different gateway addresses in their `ModelConfig`.

agentgateway v2.2.1 `AgentgatewayPolicy` `targetRefs` supports `kind: HTTPRoute` — so granularity is as fine as your routing rules.

---

## 12. Can I implement custom cost controls?

**Yes — multiple approaches, increasing in complexity:**

**1. Declarative (no code):** `AgentgatewayPolicy` token budgets. Define once in YAML, enforced by the proxy on every request. Zero application-level changes.

**2. Phoenix alerting:** Query the Phoenix API for cumulative token spend per agent/project. Trigger a webhook or PagerDuty alert when a threshold is crossed. The agent itself stops when the Kubernetes HPA or a custom controller scales it down.

**3. Custom FastMCP middleware:** In `mcp_server_traced.py`, add a token-counting decorator around tool handlers. Maintain a running total in a shared counter (Redis, or a Kubernetes ConfigMap). Raise an error when budget is exhausted.

```python
@mcp.tool()
def fetch_page(url: str) -> str:
    if get_tokens_used() > TOKEN_BUDGET:
        raise RuntimeError("Agent token budget exhausted")
    # ... actual implementation
```

**4. OPA/Gatekeeper admission policies:** Prevent deployment of Agent CRDs that don't declare a `costBudget` annotation. Enforce naming conventions that map to gateway policy tiers.

---

## 13. Per-agent budgets or depth of Token limits

**Token budget depth** in our setup:

```
Request → agentgateway-proxy
           ├─ [Global Gateway budget]  ← shared across all agents
           ├─ [Per-HTTPRoute budget]   ← per agent class
           └─ [Per-Backend budget]     ← caps total spend on OpenAI backend
```

**Tool call depth / recursion limits:** This is handled at two layers:

`mcp-governance` (`MCPGovernancePolicy` in `releases/mcpg.yaml`) scans the `kagent` namespace and can enforce maximum tool calls per session, call frequency limits, and tool pattern allowlists/denylists.

`AgentgatewayPolicy` on the MCP gateway adds a second layer: it can restrict which specific MCP **tools by name** are accessible through the gateway, using CEL expressions:

```yaml
apiVersion: agentgateway.dev/v1alpha1
kind: AgentgatewayPolicy
metadata:
  name: tool-rbac
  namespace: mcp
spec:
  targetRefs:
    - kind: AgentgatewayBackend
      name: github-mcp-backend
  backend:
    mcp:
      authorization:
        action: Allow
        policy:
          matchExpressions:
            - 'mcp.tool.name in ["fetch", "search"]'  # only these tools allowed
```

This gives fine-grained RBAC at the tool call level — an agent can only invoke the tools you explicitly allow, even if the MCP server exposes many more.

For a concrete depth limit: kagent's agent loop itself has a configurable `maxMessages` or `maxIterations` concept (set in the Agent spec or system message). Combined with agentgateway's token budget, this creates a hard ceiling on both depth and cost.

---

## 14. vLLM suitable for agents with many back-and-forth tool calls, or better for single-shot inference?

**vLLM is primarily a throughput optimizer — not a latency optimizer.**

vLLM's core innovations (PagedAttention, continuous batching, speculative decoding) maximize tokens-per-second across many concurrent requests. For a single agentic chain making 15 sequential LLM calls:

**Where vLLM helps:**
- **Prefix caching (`--enable-prefix-caching`):** The system prompt and few-shot examples are identical across all 15 calls from the same agent. vLLM caches the KV state for this prefix, dramatically reducing prefill time on calls 2–15 (from ~300ms to ~20ms for the cached portion).
- **Parallel agent populations:** If 50 agents are all making tool calls simultaneously, vLLM's batching beats naive single-request serving.

**Where vLLM struggles for agents:**
- **Sequential latency:** Each tool call must complete before the next starts. vLLM doesn't help with this fundamental serialization. Decode latency per token is not vLLM's strength.
- **Small batch sizes:** A single agent making sequential calls often has batch size = 1, where continuous batching provides no benefit.

**Recommendation:** For agentic workloads, vLLM with `--enable-prefix-caching` and `--max-model-len` tuned to your context window is a solid choice — but pair it with llm-d (see Q15) for the scheduling benefits. For pure single-agent latency sensitivity, llama.cpp or TensorRT-LLM may serve better.

**agentgateway + InferencePool** (from the Kubernetes Inference Gateway API) is the native integration point. When agentgateway routes to a `vLLM InferencePool`, it can factor in KV-cache state, GPU utilization, and queue depth when selecting which node to route a request to — addressing the sequential-latency problem at the infrastructure layer rather than the application layer.

In our current setup we use OpenAI directly. If moving to self-hosted inference, vLLM v0.6+ with prefix caching enabled + agentgateway InferencePool routing would be the right starting point.

---

## 15. llm-d's scheduler — helps when an agent makes 15 LLM calls?

**Yes — this is exactly the use case llm-d is designed for.**

**What llm-d is:** A Kubernetes scheduler extension (gateway + inference scheduler) that is KV-cache-aware. It routes incoming LLM requests to the inference server instance that already has the relevant KV cache state in GPU memory.

**Why it matters for agentic workloads:**

An agent making 15 sequential LLM calls has a growing conversation:
```
Call 1:  [System prompt] + [User turn 1]
Call 2:  [System prompt] + [User turn 1] + [Tool result 1] + [User turn 2]
...
Call 15: [System prompt] + ... + [All previous turns]
```

Every call **shares a common prefix** (the system prompt + prior history). Without llm-d, a load balancer might route Call 2 to a different GPU than Call 1 — the KV cache from Call 1 is lost, and the full prefix must be recomputed.

**With llm-d:** The scheduler tracks which nodes have which KV cache prefixes (via hash-based routing). Call 2 is routed to the same node as Call 1. The shared prefix is already in the KV cache — only the new tokens need prefill. For a 15-call chain with a 4K-token system prompt:

- Without prefix caching + llm-d: 15 × 4K-token prefill = 60K tokens of redundant compute
- With prefix caching + llm-d: 1 × 4K-token prefill + 14 × ~50-token incremental prefill

This can reduce total latency for the chain by 40–60%, and GPU compute cost proportionally.

**In our stack:** We don't currently deploy vLLM or llm-d (all traffic goes to OpenAI). If self-hosting LLMs becomes part of the stack, the recommended architecture would be:

```
kagent agents → agentgateway-proxy → llm-d scheduler → vLLM instances (with prefix caching)
```

llm-d is a CNCF sandbox project and integrates naturally with Kubernetes-native deployments like ours.

---
