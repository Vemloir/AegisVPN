# Graph Report - VPN  (2026-06-22)

## Corpus Check
- 89 files · ~49,002 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 903 nodes · 1958 edges · 49 communities (42 shown, 7 thin omitted)
- Extraction: 86% EXTRACTED · 14% INFERRED · 0% AMBIGUOUS · INFERRED: 277 edges (avg confidence: 0.68)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `5d6029e3`
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
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]

## God Nodes (most connected - your core abstractions)
1. `t()` - 63 edges
2. `SubscriptionService` - 54 edges
3. `is_admin()` - 39 edges
4. `InlineKeyboardButton` - 34 edges
5. `AgentClient` - 29 edges
6. `get_user_language()` - 29 edges
7. `ServerAccessService` - 23 edges
8. `Base` - 22 edges
9. `AsyncSession` - 21 edges
10. `AdminStates` - 20 edges

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

## Communities (49 total, 7 thin omitted)

### Community 0 - "Admin Panel & Controls"
Cohesion: 0.07
Nodes (87): fmt_bytes(), is_admin(), Shared helpers for the admin handlers., Admin panel handlers, split by domain into a small package.  The public surface, admin_back_keyboard(), admin_panel_keyboard(), admin_stats_keyboard(), build_duplicate_name_keys() (+79 more)

### Community 1 - "User Bot Handlers"
Cohesion: 0.16
Nodes (29): InlineKeyboardMarkup, CallbackQuery, Message, t(), delete_account_keyboard(), device_detail_keyboard(), device_remove_confirm_keyboard(), devices_list_keyboard() (+21 more)

### Community 2 - "Agent API & Connection Limits"
Cohesion: 0.16
Nodes (20): conn_limit_loop(), enforce_conn_limit_once(), _limit_for(), online_ips(), online_users(), Per-subscription simultaneous-connection limit.  Xray's StatsService reports eac, Block source IPs that exceed the per-subscription limit.      For each online us, Emails of users with at least one live session right now. (+12 more)

### Community 3 - "Bot Service Layer"
Cohesion: 0.13
Nodes (9): AsyncSession, Subscription, The stored (protocol, transport) for one location, or the default         (vless, Upsert a per-location preference. Selecting the plain default         (vless/xht, Drop a location's preference, returning it to vless/xhttp., A plausible OS version from a raw UA token, or '' if implausible.          Guard, Client build number from the UA, if present.          Many clients append a long, Reissue a subscription for a user.          Deactivates the old active subscript (+1 more)

### Community 4 - "Bot App Entrypoint"
Cohesion: 0.10
Nodes (39): Application, AppRunner, AsyncIOScheduler, BaseMiddleware, Bot, Any, TelegramObject, Bot (+31 more)

### Community 5 - "Bot Core & Bootstrap"
Cohesion: 0.12
Nodes (15): Settings, BaseSettings, Path, AsyncSession, bootstrap_application(), bootstrap_plans(), bootstrap_server(), ensure_default_plan_exists() (+7 more)

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
Cohesion: 0.28
Nodes (16): datetime, DeclarativeBase, Base, utcnow(), Device, Payment, Plan, Referral (+8 more)

### Community 10 - "User Service Logic"
Cohesion: 0.10
Nodes (15): datetime, _now(), pick_language(), Business logic for the end-user flow (registration, privacy, trial, account).  P, Create the user on first /start (or refresh username on return).          Return, Mark the policy accepted. Returns ``(language, can_use_trial)``., Revoke access on all nodes for every subscription, then delete the         user, Rotate sub_token + client_uuid so old URL and VLESS credentials stop working. (+7 more)

### Community 11 - "VPS Update & Deploy"
Cohesion: 0.17
Nodes (26): Namespace, Path, SFTPClient, SSHClient, connect(), get_sftp(), main(), parse_args() (+18 more)

### Community 12 - "Subscription Service Tests"
Cohesion: 0.11
Nodes (16): Server, Subscription, _greece_server(), A user with no preference (transport=None) gets EXACTLY the same link as     bef, _sub(), test_default_transport_is_byte_identical(), test_greece_offers_all_three_transports(), test_grpc_transport_uses_grpc_port_and_service_name() (+8 more)

### Community 13 - "GeoIP Lookup Service"
Cohesion: 0.17
Nodes (13): ensure_db(), flag_emoji(), _get_reader(), _is_fresh(), _localized(), lookup(), _months_to_try(), Offline GeoIP: resolve an approximate city/country from an IP, fully locally.  U (+5 more)

### Community 14 - "Community 14"
Cohesion: 0.13
Nodes (15): authenticate(), kick(), online(), Local Hysteria2 control plane.  Hysteria2 runs as a separate process and authent, Hy2 ids (emails) with at least one live session right now., Rebuild the valid Hy2 user set (uuid -> email) from an xray config., Load the on-disk xray config and rebuild the Hy2 user set from it., Hy2 auth callback result for a given secret (the client's xray UUID). (+7 more)

### Community 16 - "Telegraph Privacy Page"
Cohesion: 0.07
Nodes (39): AsyncSession, Column, _existing_columns(), _is_sqlite(), Lightweight, idempotent schema migrations applied on startup.  The project delib, Add any columns missing from the live schema. Idempotent., run_migrations(), _api() (+31 more)

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
Cohesion: 0.11
Nodes (25): InlineKeyboardMarkup, Message, Any, Message, TelegramObject, _is_start_command(), Global legal-acceptance gate.  Until a user accepts the current TERMS_VERSION (P, Acceptance gate keyboard: two document URL buttons + one accept button, with gra (+17 more)

### Community 34 - "Community 34"
Cohesion: 0.11
Nodes (17): Access Control, Adding a New Server, Additional VPN servers, Admin Panel, Aegis VPN, Agent Responsibilities, Bot Responsibilities, Current Architecture (+9 more)

### Community 35 - "Community 35"
Cohesion: 0.10
Nodes (20): 10. Возврат денежных средств, 11. Конфиденциальность и обработка данных, 12. Реферальная программа, 13. Ограничение ответственности и возмещение убытков, 14. Обстоятельства непреодолимой силы (форс-мажор), 15. Противодействие отмыванию средств и санкционные ограничения (AML), 16. Приостановление и прекращение оказания услуг, 17. Изменение Соглашения (+12 more)

### Community 36 - "Community 36"
Cohesion: 0.15
Nodes (20): CallbackQuery, InlineKeyboardMarkup, Message, CallbackQuery, End-user handlers, split by domain into a small package.  Public surface is :dat, subscription_keyboard(), cq_privacy_accept(), cq_privacy_show() (+12 more)

### Community 38 - "Community 38"
Cohesion: 0.24
Nodes (16): CallbackQuery, InlineKeyboardMarkup, Message, Returns True if the user may proceed; otherwise answers an alert., require_privacy(), cmd_subscription(), cq_help_setup(), cq_subscription_open() (+8 more)

### Community 39 - "Community 39"
Cohesion: 0.19
Nodes (7): language_label(), normalize_language(), set_user_language(), test_language_label(), test_t_falls_back_to_ru_for_unknown_language(), test_t_formats_kwargs(), test_t_returns_translation()

### Community 40 - "Community 40"
Cohesion: 0.38
Nodes (13): CallbackQuery, get_user_language(), _clean_label(), cq_device_detail(), cq_device_resume(), cq_device_suspend(), cq_devices_open(), cq_devices_remove() (+5 more)

### Community 41 - "Community 41"
Cohesion: 0.21
Nodes (12): Bot, CallbackQuery, InlineKeyboardMarkup, Message, Plan, create_invoice(), no_plans_keyboard(), plan_selection_keyboard() (+4 more)

### Community 42 - "Community 42"
Cohesion: 0.16
Nodes (7): Any, ClientSession, AgentClient, get_session(), Per-email traffic counters from the node's Xray.          Returns ``{email: {", Push a per-user connection-limit override to the node.          ``limit`` None, Emails with at least one live session on this node right now.          Returns

### Community 44 - "Community 44"
Cohesion: 0.14
Nodes (7): Subscription, User, AdminService, AdminStats, Business logic for the admin panel.  Pure data access and state mutations — no a, Resolve an admin lookup query (numeric Telegram ID or @username) to a         tg, Set a user's connection-limit override and push it to every node.          ``lim

### Community 45 - "Community 45"
Cohesion: 0.15
Nodes (7): Server, Collapse a stored (protocol, transport) preference into the concrete         VLE, Map ``server_id -> concrete VLESS transport`` for this user's stored         pre, Normalize the agent's raw vless link onto the bot's authoritative         params, Full xray-JSON subscription (array of standalone configs, one per         server, Parse one normalized vless:// link into a complete, standalone xray         clie, VLESS transports this server can serve, in display order. Always         include

### Community 46 - "Community 46"
Cohesion: 0.25
Nodes (13): CallbackQuery, Server, locations_list_keyboard(), One button per active location (flag + name), then back-to-settings., _active_servers_for(), cq_location_hy2_disabled(), cq_location_open(), cq_location_reset() (+5 more)

### Community 47 - "Community 47"
Cohesion: 0.33
Nodes (5): AsyncSession, Server, Subscription, User, ServerAccessService

### Community 48 - "Community 48"
Cohesion: 0.33
Nodes (9): _fresh_db(), Per-location transport preferences: storage, default-means-no-row, reset, and th, Create one user + one Greece-like server. Returns (user_id, server_id)., _seed(), test_default_means_no_row(), test_reset_clears_a_locations_pref(), test_selecting_default_deletes_the_row(), test_set_grpc_pref_round_trips_into_build_map() (+1 more)

## Knowledge Gaps
- **98 isolated node(s):** `AsyncSession`, `Column`, `AsyncSession`, `Router`, `Plan` (+93 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `bootstrap_application()` connect `Bot Core & Bootstrap` to `Telegraph Privacy Page`, `Bot App Entrypoint`?**
  _High betweenness centrality (0.191) - this node is a cross-community bridge._
- **Why does `run()` connect `Bot App Entrypoint` to `Bot Core & Bootstrap`?**
  _High betweenness centrality (0.177) - this node is a cross-community bridge._
- **Are the 59 inferred relationships involving `t()` (e.g. with `create_invoice()` and `no_plans_keyboard()`) actually correct?**
  _`t()` has 59 INFERRED edges - model-reasoned connections that need verification._
- **Are the 18 inferred relationships involving `SubscriptionService` (e.g. with `CallbackQuery` and `InlineKeyboardMarkup`) actually correct?**
  _`SubscriptionService` has 18 INFERRED edges - model-reasoned connections that need verification._
- **Are the 31 inferred relationships involving `InlineKeyboardButton` (e.g. with `admin_back_keyboard()` and `admin_panel_keyboard()`) actually correct?**
  _`InlineKeyboardButton` has 31 INFERRED edges - model-reasoned connections that need verification._
- **Are the 17 inferred relationships involving `AgentClient` (e.g. with `Subscription` and `User`) actually correct?**
  _`AgentClient` has 17 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Per-subscription simultaneous-connection limit.  Xray's StatsService reports eac`, `Set (or, when limit is None, clear) a user's connection-limit override.`, `Emails of users with at least one live session right now.` to the rest of the system?**
  _239 weakly-connected nodes found - possible documentation gaps or missing edges._