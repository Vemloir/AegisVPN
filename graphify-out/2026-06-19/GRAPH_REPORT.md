# Graph Report - VPN  (2026-06-19)

## Corpus Check
- 83 files · ~37,505 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 753 nodes · 1657 edges · 32 communities (25 shown, 7 thin omitted)
- Extraction: 87% EXTRACTED · 13% INFERRED · 0% AMBIGUOUS · INFERRED: 219 edges (avg confidence: 0.67)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `34cd7e5b`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Admin Panel & Controls|Admin Panel & Controls]]
- [[_COMMUNITY_User Bot Handlers|User Bot Handlers]]
- [[_COMMUNITY_Agent API & Connection Limits|Agent API & Connection Limits]]
- [[_COMMUNITY_Bot Service Layer|Bot Service Layer]]
- [[_COMMUNITY_Bot App Entrypoint|Bot App Entrypoint]]
- [[_COMMUNITY_Bot Core & Bootstrap|Bot Core & Bootstrap]]
- [[_COMMUNITY_Project Docs & Architecture|Project Docs & Architecture]]
- [[_COMMUNITY_VPS Server Provisioning|VPS Server Provisioning]]
- [[_COMMUNITY_Scheduler & Background Tasks|Scheduler & Background Tasks]]
- [[_COMMUNITY_Database Models|Database Models]]
- [[_COMMUNITY_User Service Logic|User Service Logic]]
- [[_COMMUNITY_VPS Update & Deploy|VPS Update & Deploy]]
- [[_COMMUNITY_Subscription Service Tests|Subscription Service Tests]]
- [[_COMMUNITY_GeoIP Lookup Service|GeoIP Lookup Service]]
- [[_COMMUNITY_Amnezia Node Sync|Amnezia Node Sync]]
- [[_COMMUNITY_Telegraph Privacy Page|Telegraph Privacy Page]]
- [[_COMMUNITY_Traffic Polling Tests|Traffic Polling Tests]]
- [[_COMMUNITY_Amnezia Peer Config Apply|Amnezia Peer Config Apply]]
- [[_COMMUNITY_Amnezia Peer Sync|Amnezia Peer Sync]]
- [[_COMMUNITY_Agent Entrypoint|Agent Entrypoint]]
- [[_COMMUNITY_Amnezia Node Setup|Amnezia Node Setup]]
- [[_COMMUNITY_Amnezia Server Setup|Amnezia Server Setup]]
- [[_COMMUNITY_Agent Test Fixtures|Agent Test Fixtures]]
- [[_COMMUNITY_Bot Test Fixtures|Bot Test Fixtures]]
- [[_COMMUNITY_Claude Config|Claude Config]]
- [[_COMMUNITY_Serena Config|Serena Config]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 37|Community 37]]

## God Nodes (most connected - your core abstractions)
1. `t()` - 50 edges
2. `SubscriptionService` - 42 edges
3. `is_admin()` - 39 edges
4. `AgentClient` - 30 edges
5. `InlineKeyboardButton` - 29 edges
6. `get_user_language()` - 22 edges
7. `ServerAccessService` - 21 edges
8. `AdminStates` - 20 edges
9. `Base` - 20 edges
10. `AdminService` - 20 edges

## Surprising Connections (you probably didn't know these)
- `Architecture Documentation` --semantically_similar_to--> `Project README`  [INFERRED] [semantically similar]
  ARCHITECTURE.md → README.md
- `Telegram Stars Payments` --conceptually_related_to--> `Bot Component`  [INFERRED]
  bot/src/privacy/privacy_en.md → ARCHITECTURE.md
- `CI Workflow` --references--> `Agent Component`  [EXTRACTED]
  .github/workflows/ci.yml → ARCHITECTURE.md
- `CI Workflow` --references--> `Bot Component`  [EXTRACTED]
  .github/workflows/ci.yml → ARCHITECTURE.md
- `Bot Docker Compose (dev)` --references--> `Bot Component`  [EXTRACTED]
  bot/docker-compose.yml → ARCHITECTURE.md

## Import Cycles
- 1-file cycle: `bot/src/handlers/admin/__init__.py -> bot/src/handlers/admin/__init__.py`
- 1-file cycle: `bot/src/handlers/user/__init__.py -> bot/src/handlers/user/__init__.py`
- 1-file cycle: `bot/src/models/base.py -> bot/src/models/base.py`
- 1-file cycle: `bot/src/services/user_service.py -> bot/src/services/user_service.py`

## Hyperedges (group relationships)
- **Main VPS Production Stack (vpn + bot + caddy)** — concept_agent_component, concept_bot_component, concept_caddy_proxy, vps_docker_compose [EXTRACTED 1.00]
- **Subscription Delivery Pipeline** — concept_bot_component, concept_agent_component, concept_subscription_flow, concept_vless_reality [EXTRACTED 0.95]
- **CI Lint and Test Matrix (bot + agent)** — ci_yml, concept_bot_component, concept_agent_component [EXTRACTED 1.00]

## Communities (32 total, 7 thin omitted)

### Community 0 - "Admin Panel & Controls"
Cohesion: 0.07
Nodes (86): fmt_bytes(), is_admin(), Shared helpers for the admin handlers., Admin panel handlers, split by domain into a small package.  The public surface, admin_back_keyboard(), admin_panel_keyboard(), admin_stats_keyboard(), build_duplicate_name_keys() (+78 more)

### Community 1 - "User Bot Handlers"
Cohesion: 0.05
Nodes (89): Bot, CallbackQuery, InlineKeyboardMarkup, Message, Plan, CallbackQuery, InlineKeyboardMarkup, CallbackQuery (+81 more)

### Community 2 - "Agent API & Connection Limits"
Cohesion: 0.05
Nodes (79): HTTPAuthorizationCredentials, HTTPAuthorizationCredentials, conn_limit_loop(), enforce_conn_limit_once(), _limit_for(), online_ips(), online_users(), Per-subscription simultaneous-connection limit.  Xray's StatsService reports eac (+71 more)

### Community 3 - "Bot Service Layer"
Cohesion: 0.05
Nodes (27): Message, Subscription, User, Any, AsyncSession, Server, Subscription, User (+19 more)

### Community 4 - "Bot App Entrypoint"
Cohesion: 0.10
Nodes (37): Application, AppRunner, AsyncIOScheduler, BaseMiddleware, Bot, Any, Bot, Dispatcher (+29 more)

### Community 5 - "Bot Core & Bootstrap"
Cohesion: 0.08
Nodes (25): Settings, BaseSettings, Path, AsyncSession, AsyncSession, bootstrap_application(), bootstrap_plans(), bootstrap_server() (+17 more)

### Community 6 - "Project Docs & Architecture"
Cohesion: 0.09
Nodes (39): Agent Docker Compose, Aegis VPN — Node Agent, API, Development, Layout, Bootstrap Logic, Architecture Documentation, Bot Docker Compose (dev) (+31 more)

### Community 7 - "VPS Server Provisioning"
Cohesion: 0.14
Nodes (27): Namespace, Path, SFTPClient, SSHClient, build_setup_script(), connect(), ensure_remote_dir(), exec_command() (+19 more)

### Community 8 - "Scheduler & Background Tasks"
Cohesion: 0.14
Nodes (22): Bot, InlineKeyboardMarkup, Path, Server, _alert_admins(), backup_database(), check_expired_subscriptions(), _check_one() (+14 more)

### Community 9 - "Database Models"
Cohesion: 0.36
Nodes (13): datetime, DeclarativeBase, Base, utcnow(), Device, Payment, Plan, Referral (+5 more)

### Community 10 - "User Service Logic"
Cohesion: 0.12
Nodes (13): datetime, _now(), pick_language(), Business logic for the end-user flow (registration, privacy, trial, account).  P, Mark the policy accepted. Returns ``(language, can_use_trial)``., Revoke access on all nodes for every subscription, then delete the         user, Rotate sub_token + client_uuid so old URL and VLESS credentials stop working., Activate the free trial. Returns ``(language, status)`` where status is (+5 more)

### Community 11 - "VPS Update & Deploy"
Cohesion: 0.18
Nodes (23): Namespace, Path, SFTPClient, SSHClient, connect(), get_sftp(), main(), parse_args() (+15 more)

### Community 12 - "Subscription Service Tests"
Cohesion: 0.16
Nodes (5): Subscription, _sub(), test_is_lifetime_by_expires_at(), test_is_lifetime_by_plan_days(), test_regular_subscription_is_not_lifetime()

### Community 13 - "GeoIP Lookup Service"
Cohesion: 0.17
Nodes (13): ensure_db(), flag_emoji(), _get_reader(), _is_fresh(), _localized(), lookup(), _months_to_try(), Offline GeoIP: resolve an approximate city/country from an IP, fully locally.  U (+5 more)

### Community 15 - "Amnezia Node Sync"
Cohesion: 0.44
Nodes (8): Connection, fetch_peers_for_server(), fetch_servers(), main(), resolve_node_host(), run(), sync_local(), sync_remote()

### Community 16 - "Telegraph Privacy Page"
Cohesion: 0.33
Nodes (8): _api(), get_privacy_url(), _load(), Publish the privacy policy as a Telegraph page so it opens as a clean in-app pag, Convert our Telegram-HTML-ish privacy text into Telegraph DOM nodes., Return a Telegraph URL for the policy. Creates the page once, and edits     it i, _save(), _to_nodes()

### Community 17 - "Traffic Polling Tests"
Cohesion: 0.47
Nodes (8): _patch_stats(), poll_traffic must aggregate per-device emails (user_X_sub_Y_dev_Z), not just the, Create a user, subscription, server and link. Returns (sub_id, email_prefix)., _seed(), test_base_email_still_counted(), test_per_device_emails_are_aggregated(), test_xray_restart_counts_current_value(), _totals()

### Community 18 - "Amnezia Peer Config Apply"
Cohesion: 0.50
Nodes (7): Path, build_configs(), interface_exists(), main(), read_env_file(), read_peers(), run()

### Community 19 - "Amnezia Peer Sync"
Cohesion: 0.50
Nodes (7): Path, build_configs(), fetch_active_peers(), interface_exists(), main(), read_env_file(), run()

### Community 20 - "Agent Entrypoint"
Cohesion: 0.83
Nodes (3): run_uvicorn_loop(), run_xray_loop(), entrypoint.sh script

### Community 30 - "Community 30"
Cohesion: 0.09
Nodes (22): 1. Bot, 2. Agent, 3. Xray, A. User buys or renews VPN, Access Control, Adding a New Server, Additional VPN VPS nodes, Agent config (+14 more)

### Community 34 - "Community 34"
Cohesion: 0.11
Nodes (17): Access Control, Adding a New Server, Additional VPN servers, Admin Panel, Aegis VPN, Agent Responsibilities, Bot Responsibilities, Current Architecture (+9 more)

## Knowledge Gaps
- **79 isolated node(s):** `AsyncSession`, `Column`, `AsyncSession`, `Router`, `Plan` (+74 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `bootstrap_application()` connect `Bot Core & Bootstrap` to `Bot App Entrypoint`?**
  _High betweenness centrality (0.193) - this node is a cross-community bridge._
- **Why does `run()` connect `Bot App Entrypoint` to `Bot Core & Bootstrap`?**
  _High betweenness centrality (0.189) - this node is a cross-community bridge._
- **Are the 46 inferred relationships involving `t()` (e.g. with `create_invoice()` and `no_plans_keyboard()`) actually correct?**
  _`t()` has 46 INFERRED edges - model-reasoned connections that need verification._
- **Are the 15 inferred relationships involving `SubscriptionService` (e.g. with `CallbackQuery` and `CallbackQuery`) actually correct?**
  _`SubscriptionService` has 15 INFERRED edges - model-reasoned connections that need verification._
- **Are the 17 inferred relationships involving `AgentClient` (e.g. with `Subscription` and `User`) actually correct?**
  _`AgentClient` has 17 INFERRED edges - model-reasoned connections that need verification._
- **Are the 26 inferred relationships involving `InlineKeyboardButton` (e.g. with `admin_back_keyboard()` and `admin_panel_keyboard()`) actually correct?**
  _`InlineKeyboardButton` has 26 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Per-subscription simultaneous-connection limit.  Xray's StatsService reports eac`, `Set (or, when limit is None, clear) a user's connection-limit override.`, `Emails of users with at least one live session right now.` to the rest of the system?**
  _173 weakly-connected nodes found - possible documentation gaps or missing edges._