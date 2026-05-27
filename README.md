# Astra

A native Frappe custom app that adds a floating Desk chat widget backed by a local Ollama instance.

## Install

Place this app in your bench `apps` directory, then run:

```bash
bench --site your-site install-app astra
bench --site your-site migrate
bench build --app astra
bench restart
```

Configure `Ollama Settings` in Desk. For self-hosted benches, Astra can call local Ollama. For Frappe Cloud, use `Remote Ollama` with a secured HTTPS endpoint to your Ollama server or reverse proxy.

Astra creates default `Astra User` and `Astra Admin` roles during installation, seeds `Ollama Settings`, and attempts to add an Astra Workspace with shortcuts to settings, knowledge base, and chat sessions.

`Astra User` can use chat/session features. `Astra Admin` can manage Astra settings, knowledge base entries, and tool confirmations. `System Manager` keeps full administrative access.

## ERPNext / Frappe v16

Astra is structured as a standard Frappe custom app and avoids core file changes, so it is intended to install on ERPNext/Frappe v16 benches. The official Frappe v16 prerequisites currently list Python 3.14, Node 24, and MariaDB 11.8; use the same runtime versions as your v16 bench.

After installing or updating Astra on v16, run `bench --site your-site migrate`, `bench build --app astra`, and restart the bench so DocTypes, patches, scheduler jobs, and Desk assets are all active. Assign `Astra User` to normal chat users and `Astra Admin` to users who can manage knowledge, confirmations, evaluations, and observability.

For realtime streaming and background alerts, keep the standard Frappe services running: web, workers, scheduler, Redis, and Socket.IO. Ollama must be reachable from the Frappe server at the URL configured in `Ollama Settings`.

## Ollama Provider Setup

For a private server where Ollama runs beside Frappe:

```text
Provider: Local Ollama
API URL: http://localhost:11434
```

For Frappe Cloud:

```text
Provider: Remote Ollama
API URL: https://your-secure-ollama-domain.example.com
Remote Auth Type: None
API Key: blank unless your reverse proxy requires a bearer token
Allow Remote Business Context: enabled only for a private, trusted endpoint
```

If your reverse proxy requires bearer authentication, use:

```text
Remote Auth Type: Bearer Token
API Key: your bearer token
```

If you specifically protected the tunnel with Cloudflare Access, create a Cloudflare Access service token and use:

```text
Remote Auth Type: Cloudflare Access Service Token
Cloudflare Access Client ID: your service token client ID
Cloudflare Access Client Secret: your service token client secret
```

Cloudflare Access is optional. For a regular Cloudflare Tunnel without Access, keep `Remote Auth Type` as `None` or `Bearer Token` depending on your own proxy, and make sure Cloudflare Bot/WAF rules allow server-to-server POST requests to Ollama paths such as `/api/chat`, `/api/generate`, `/api/embed`, `/api/embeddings`, and `/api/tags`.

Do not expose raw `http://server-ip:11434` to the public internet. Put Docker Ollama behind HTTPS and access control, such as Caddy/Nginx with bearer auth, Cloudflare Tunnel with suitable WAF rules, Cloudflare Access service-token auth, or a private VPN/tunnel that Frappe Cloud can reach.

## Optional Frappe Assistant Core Tools

Astra can call Frappe Assistant Core tools when FAC is installed on the same site:

```bash
bench get-app https://github.com/buildswithpaul/Frappe_Assistant_Core
bench --site your-site install-app frappe_assistant_core
bench --site your-site install-app astra
bench --site your-site migrate
bench build --app astra
```

In `Ollama Settings`, enable `Enable Frappe Assistant Core Tools`. Astra exposes only the tools in `FAC Tool Allowlist`, validates arguments against cached FAC schemas, and routes write actions through `Astra Tool Confirmation`.

Astra can make multiple FAC tool calls for one user message. For example, it can discover reports with `report_list`, inspect requirements with `report_requirements`, run the report with `generate_report`, then summarize the result through Ollama. `Max FAC Tool Calls` controls the per-message limit.

Check the bridge from bench:

```bash
bench --site your-site execute astra.api.get_fac_status
```

Check overall assistant status:

```bash
bench --site your-site execute astra.api.get_assistant_status
```

Run the broader install smoke test:

```bash
bench --site your-site execute astra.diagnostics.run_smoke_test
```

## Chat History

Astra stores conversations in `Astra Chat Session` and `Astra Chat Message`. Messages include the assistant response, model name, sources, and compact FAC tool trace data. The Desk widget remembers the active session in browser storage and reloads it after a page refresh.

The Desk widget also includes a session picker, rename action, close action, and new-chat action.

## Streaming Responses

Plain Ollama responses stream through Frappe realtime events. FAC tool mode now streams status updates, pauses for read tools or write confirmations, then streams the final answer after tool results are collected.

## Write Action Confirmation

FAC write/action tools are not executed directly during a model response. If `Enable Write Action Confirmation` is enabled in `Ollama Settings` and the relevant tool is included in `FAC Tool Allowlist`, Astra creates an `Astra Tool Confirmation` record and shows Approve/Reject buttons in the chat. Approval executes the tool server-side after ownership and basic Frappe permission checks.

Write confirmations store a JSON preview with DocType, target document, and field before/after values when Astra can infer them.

Tools that execute Python, SQL, analytics, files, or dashboard creation remain blocked.

## Documentation RAG

Astra can ingest official Frappe/ERPNext documentation into `AI Knowledge Base`, chunk it, tag source groups/doc versions, score chunk quality, mark stale chunks, and create local Ollama embeddings. The default embedding model is `nomic-embed-text`.

The widget's `Admin` button opens a Desk dialog for URL ingestion, pasted text ingestion, embedding rebuild, workflow-pack seeding, and observability summary access.

Pull the embedding model first:

```bash
ollama pull nomic-embed-text
```

Ingest a documentation URL:

```bash
bench --site your-site execute astra.rag.ingest_knowledge_base_url --kwargs "{'url': 'https://docs.frappe.io/framework/user/en/introduction', 'category': 'Frappe Framework'}"
```

Ingest text directly:

```bash
bench --site your-site execute astra.rag.ingest_knowledge_base_text --kwargs "{'title': 'Delivery Note Notes', 'category': 'ERPNext', 'content': 'Your markdown content here'}"
```

Rebuild embeddings for existing knowledge chunks:

```bash
bench --site your-site execute astra.rag.rebuild_knowledge_base_embeddings
```

Only System Managers can run ingestion. URL ingestion is restricted to official Frappe and ERPNext documentation hosts.

Mark stale chunks before re-ingesting a changed source group:

```bash
bench --site your-site execute astra.rag.mark_knowledge_base_stale --kwargs "{'source_group': 'framework/user'}"
```

## Tests

Run Astra's focused permission/security tests from a bench:

```bash
bench --site your-site run-tests --app astra --module astra.tests.test_security
```

## Intelligence Foundations

Astra includes:

- intent routing for docs, schema, live ERP data, reports, write actions, and admin troubleshooting
- schema caching for DocType metadata and FAC tool schemas
- FAC tool contract snapshots in `Astra FAC Tool Contract`, including schema hashes and changed/missing status
- dedicated `Astra Tool Event` audit records for intent, plan, validation, repair, confirmation, execution, and final-answer events
- role-specific ERPNext action packs for Accounts, Sales, Stock, Purchase, and Manufacturing users
- current Desk document context when the user is viewing a form
- contextual Desk tips for workflow, permission, mandatory-field, and linked-record errors
- FAC tool argument validation and repair prompts
- per-user preferences in `Astra User Preferences`
- prompt/policy version records in `Astra Policy Version`
- prompt/policy evaluations in `Astra Evaluation Case`, with optional live model checks
- configurable background alert rules in `Astra Alert Rule`
- document creation and report filter mini-wizards in the Desk chat widget, including Link-field validation, draft preview, and JSON child-table rows
- report result drilldown from preview rows when a result includes a linked DocType/name
- tool transcript viewing for admins: intent, plan, validation, tool calls, results, and final messages
- observability summary via `astra.api.get_observability_summary`
- install doctor via `bench --site your-site execute astra.install_doctor.run`

## Migration Notes

When updating an existing Astra install:

```bash
bench --site your-site migrate
bench build --app astra
bench restart
```

Then verify:

```bash
bench --site your-site execute astra.api.get_assistant_status
bench --site your-site execute astra.diagnostics.run_smoke_test
```

Optional FAC installs should be migrated before Astra status checks. If FAC is enabled but no tools are discovered, review `FAC Tool Allowlist` in `Ollama Settings` and confirm Frappe Assistant Core is installed on the same site.
