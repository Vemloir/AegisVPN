# Graph Report - VPN  (2026-06-22)

## Corpus Check
- 85 files · ~45,171 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 838 nodes · 1811 edges · 44 communities (37 shown, 7 thin omitted)
- Extraction: 86% EXTRACTED · 14% INFERRED · 0% AMBIGUOUS · INFERRED: 247 edges (avg confidence: 0.68)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `6f8ac430`
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
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Telegraph Privacy Page|Telegraph Privacy Page]]
- [[_COMMUNITY_Traffic Polling Tests|Traffic Polling Tests]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Agent Entrypoint|Agent Entrypoint]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Agent Test Fixtures|Agent Test Fixtures]]
- [[_COMMUNITY_Bot Test Fixtures|Bot Test Fixtures]]
- [[_COMMUNITY_Claude Config|Claude Config]]
- [[_COMMUNITY_Serena Config|Serena Config]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]

## God Nodes (most connected - your core abstractions)
1. `t()` - 55 edges
2. `SubscriptionService` - 45 edges
3. `is_admin()` - 39 edges
4. `InlineKeyboardButton` - 31 edges
5. `AgentClient` - 29 edges
6. `get_user_language()` - 24 edges
7. `ServerAccessService` - 21 edges
8. `AdminStates` - 20 edges
9. `Base` - 20 edges
10. `AdminService` - 20 edges

## Surprising Connections (you probably didn't know these)
- `Architecture Documentation` --semantically_similar_to--> `Project README`  [INFERRED] [semantically similar]
  ARCHITECTURE.md → README.md
- `Telegram Stars Payments` --conceptually_related_to--> `Bot Component`  [INFERRED]
  bot/src/privacy/privacy_en.md → ARCHITECTURE.md
- `Message` --uses--> `SubscriptionService`  [INFERRED]
  bot/src/handlers/user/settings.py → bot/src/services/subscription_service.py
- `test_t_falls_back_to_ru_for_unknown_language()` --calls--> `t()`  [INFERRED]
  bot/tests/test_i18n.py → bot/src/services/i18n.py
- `test_t_formats_kwargs()` --calls--> `t()`  [INFERRED]
  bot/tests/test_i18n.py → bot/src/services/i18n.py

## Import Cycles
- 1-file cycle: `bot/src/handlers/admin/__init__.py -> bot/src/handlers/admin/__init__.py`
- 1-file cycle: `bot/src/handlers/user/__init__.py -> bot/src/handlers/user/__init__.py`
- 1-file cycle: `bot/src/models/base.py -> bot/src/models/base.py`
- 1-file cycle: `bot/src/services/user_service.py -> bot/src/services/user_service.py`

## Hyperedges (group relationships)
- **Main VPS Production Stack (vpn + bot + caddy)** — concept_agent_component, concept_bot_component, concept_caddy_proxy, vps_docker_compose [EXTRACTED 1.00]
- **Subscription Delivery Pipeline** — concept_bot_component, concept_agent_component, concept_subscription_flow, concept_vless_reality [EXTRACTED 0.95]
- **CI Lint and Test Matrix (bot + agent)** — ci_yml, concept_bot_component, concept_agent_component [EXTRACTED 1.00]

## Communities (44 total, 7 thin omitted)

### Community 0 - "Admin Panel & Controls"
Cohesion: 0.07
Nodes (87): fmt_bytes(), is_admin(), Shared helpers for the admin handlers., Admin panel handlers, split by domain into a small package.  The public surface, admin_back_keyboard(), admin_panel_keyboard(), admin_stats_keyboard(), build_duplicate_name_keys() (+79 more)

### Community 1 - "User Bot Handlers"
Cohesion: 0.19
Nodes (25): InlineKeyboardMarkup, CallbackQuery, Message, t(), delete_account_keyboard(), device_detail_keyboard(), device_remove_confirm_keyboard(), devices_list_keyboard() (+17 more)

### Community 2 - "Agent API & Connection Limits"
Cohesion: 0.16
Nodes (20): conn_limit_loop(), enforce_conn_limit_once(), _limit_for(), online_ips(), online_users(), Per-subscription simultaneous-connection limit.  Xray's StatsService reports eac, Block source IPs that exceed the per-subscription limit.      For each online us, Emails of users with at least one live session right now. (+12 more)

### Community 3 - "Bot Service Layer"
Cohesion: 0.05
Nodes (28): Subscription, User, Any, AsyncSession, Server, Subscription, User, AsyncSession (+20 more)

### Community 4 - "Bot App Entrypoint"
Cohesion: 0.10
Nodes (40): Application, AppRunner, AsyncIOScheduler, BaseMiddleware, Bot, Any, TelegramObject, Message (+32 more)

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
Cohesion: 0.10
Nodes (15): datetime, _now(), pick_language(), Business logic for the end-user flow (registration, privacy, trial, account).  P, Create the user on first /start (or refresh username on return).          Return, Mark the policy accepted. Returns ``(language, can_use_trial)``., Revoke access on all nodes for every subscription, then delete the         user, Rotate sub_token + client_uuid so old URL and VLESS credentials stop working. (+7 more)

### Community 11 - "VPS Update & Deploy"
Cohesion: 0.17
Nodes (26): Namespace, Path, SFTPClient, SSHClient, connect(), get_sftp(), main(), parse_args() (+18 more)

### Community 12 - "Subscription Service Tests"
Cohesion: 0.15
Nodes (5): Subscription, _sub(), test_is_lifetime_by_expires_at(), test_is_lifetime_by_plan_days(), test_regular_subscription_is_not_lifetime()

### Community 13 - "GeoIP Lookup Service"
Cohesion: 0.17
Nodes (13): ensure_db(), flag_emoji(), _get_reader(), _is_fresh(), _localized(), lookup(), _months_to_try(), Offline GeoIP: resolve an approximate city/country from an IP, fully locally.  U (+5 more)

### Community 14 - "Community 14"
Cohesion: 0.13
Nodes (15): authenticate(), kick(), online(), Local Hysteria2 control plane.  Hysteria2 runs as a separate process and authent, Hy2 ids (emails) with at least one live session right now., Rebuild the valid Hy2 user set (uuid -> email) from an xray config., Load the on-disk xray config and rebuild the Hy2 user set from it., Hy2 auth callback result for a given secret (the client's xray UUID). (+7 more)

### Community 16 - "Telegraph Privacy Page"
Cohesion: 0.12
Nodes (26): _api(), _chunk_nodes(), _get_page_url(), get_privacy_url(), get_tos_url(), _inline_children(), _load(), _put_page() (+18 more)

### Community 17 - "Traffic Polling Tests"
Cohesion: 0.47
Nodes (8): _patch_stats(), poll_traffic must aggregate per-device emails (user_X_sub_Y_dev_Z), not just the, Create a user, subscription, server and link. Returns (sub_id, email_prefix)., _seed(), test_base_email_still_counted(), test_per_device_emails_are_aggregated(), test_xray_restart_counts_current_value(), _totals()

### Community 18 - "Community 18"
Cohesion: 0.33
Nodes (7): online(), online_emails(), Emails with at least one live session right now (authoritative online state)., get_online_count(), get_online_emails(), Emails of users with at least one active session right now.      Authoritative l, Number of users with active sessions right now (xray statsgetallonlineusers).

### Community 19 - "Community 19"
Cohesion: 0.18
Nodes (17): build_client_record(), build_subscription_query(), get_transport_type(), _parse_online_users(), Extract the list of online user emails from `statsgetallonlineusers` output., test_build_client_record_grpc_has_no_flow(), test_build_client_record_tcp_has_no_flow(), test_build_client_record_xhttp_has_no_flow() (+9 more)

### Community 20 - "Agent Entrypoint"
Cohesion: 0.83
Nodes (3): run_uvicorn_loop(), run_xray_loop(), entrypoint.sh script

### Community 21 - "Community 21"
Cohesion: 0.39
Nodes (11): hy2_auth(), Hysteria2 connect-time auth callback (loopback only, no verify_token).      Hy2, ClientAddRequest, ClientRemoveRequest, ConnLimitRequest, Hy2AuthRequest, BaseModel, ClientAddRequest (+3 more)

### Community 22 - "Community 22"
Cohesion: 0.36
Nodes (7): HTTPAuthorizationCredentials, HTTPAuthorizationCredentials, Bearer-token authentication for the agent API., verify_token(), _creds(), test_verify_token_accepts_matching_token(), test_verify_token_rejects_wrong_token()

### Community 30 - "Community 30"
Cohesion: 0.09
Nodes (22): 1. Bot, 2. Agent, 3. Xray, A. User buys or renews VPN, Access Control, Adding a New Server, Additional VPN VPS nodes, Agent config (+14 more)

### Community 31 - "Community 31"
Cohesion: 0.34
Nodes (13): add_client(), bulk_add_clients(), get_fast_subscription(), get_subscription(), health(), Aegis VPN node agent: a thin HTTP control plane over a local Xray instance.  Rou, remove_client(), find_vless_inbound() (+5 more)

### Community 32 - "Community 32"
Cohesion: 0.40
Nodes (5): Set (or, when limit is None, clear) a user's connection-limit override., _save_overrides(), set_override(), conn_limit(), Set or clear a per-user simultaneous-connection override.      limit=None clears

### Community 33 - "Community 33"
Cohesion: 0.15
Nodes (21): CallbackQuery, InlineKeyboardMarkup, Message, Any, TelegramObject, cmd_info(), cq_terms_accept(), _doc_link() (+13 more)

### Community 34 - "Community 34"
Cohesion: 0.11
Nodes (17): Access Control, Adding a New Server, Additional VPN servers, Admin Panel, Aegis VPN, Agent Responsibilities, Bot Responsibilities, Current Architecture (+9 more)

### Community 35 - "Community 35"
Cohesion: 0.10
Nodes (20): 10. Возврат денежных средств, 11. Конфиденциальность и обработка данных, 12. Реферальная программа, 13. Ограничение ответственности и возмещение убытков, 14. Обстоятельства непреодолимой силы (форс-мажор), 15. Противодействие отмыванию средств и санкционные ограничения (AML), 16. Приостановление и прекращение оказания услуг, 17. Изменение Соглашения (+12 more)

### Community 36 - "Community 36"
Cohesion: 0.21
Nodes (15): CallbackQuery, InlineKeyboardMarkup, Message, subscription_keyboard(), cq_privacy_accept(), cq_privacy_show(), load_privacy_text(), privacy_button() (+7 more)

### Community 38 - "Community 38"
Cohesion: 0.24
Nodes (16): CallbackQuery, InlineKeyboardMarkup, Message, Returns True if the user may proceed; otherwise answers an alert., require_privacy(), cmd_subscription(), cq_help_setup(), cq_subscription_open() (+8 more)

### Community 39 - "Community 39"
Cohesion: 0.17
Nodes (7): language_label(), normalize_language(), set_user_language(), test_language_label(), test_t_falls_back_to_ru_for_unknown_language(), test_t_formats_kwargs(), test_t_returns_translation()

### Community 40 - "Community 40"
Cohesion: 0.38
Nodes (13): CallbackQuery, get_user_language(), _clean_label(), cq_device_detail(), cq_device_resume(), cq_device_suspend(), cq_devices_open(), cq_devices_remove() (+5 more)

### Community 41 - "Community 41"
Cohesion: 0.21
Nodes (12): Bot, CallbackQuery, InlineKeyboardMarkup, Message, Plan, create_invoice(), no_plans_keyboard(), plan_selection_keyboard() (+4 more)

### Community 42 - "Community 42"
Cohesion: 0.50
Nodes (3): setup_routers(), Router, End-user handlers, split by domain into a small package.  Public surface is :dat

## Knowledge Gaps
- **98 isolated node(s):** `AsyncSession`, `Column`, `AsyncSession`, `Router`, `Plan` (+93 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `bootstrap_application()` connect `Bot Core & Bootstrap` to `Bot App Entrypoint`?**
  _High betweenness centrality (0.200) - this node is a cross-community bridge._
- **Why does `run()` connect `Bot App Entrypoint` to `Bot Core & Bootstrap`?**
  _High betweenness centrality (0.185) - this node is a cross-community bridge._
- **Are the 51 inferred relationships involving `t()` (e.g. with `create_invoice()` and `no_plans_keyboard()`) actually correct?**
  _`t()` has 51 INFERRED edges - model-reasoned connections that need verification._
- **Are the 15 inferred relationships involving `SubscriptionService` (e.g. with `CallbackQuery` and `CallbackQuery`) actually correct?**
  _`SubscriptionService` has 15 INFERRED edges - model-reasoned connections that need verification._
- **Are the 28 inferred relationships involving `InlineKeyboardButton` (e.g. with `admin_back_keyboard()` and `admin_panel_keyboard()`) actually correct?**
  _`InlineKeyboardButton` has 28 INFERRED edges - model-reasoned connections that need verification._
- **Are the 17 inferred relationships involving `AgentClient` (e.g. with `Subscription` and `User`) actually correct?**
  _`AgentClient` has 17 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Per-subscription simultaneous-connection limit.  Xray's StatsService reports eac`, `Set (or, when limit is None, clear) a user's connection-limit override.`, `Emails of users with at least one live session right now.` to the rest of the system?**
  _218 weakly-connected nodes found - possible documentation gaps or missing edges._