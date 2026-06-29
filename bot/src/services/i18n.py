from aiogram import html
from sqlalchemy import select

from src.core.database import async_session_maker
from src.models import User

SUPPORTED_LANGUAGES = ("ru", "en")


TEXTS: dict[str, dict[str, str]] = {
    "ru": {
        "buy_vpn": "Купить VPN",
        "renew_vpn": "Продлить VPN",
        "trial_vpn": "Пробный период 3 дня",
        "setup_guide": "Инструкция",
        "privacy_button": "Политика конфиденциальности",
        "privacy_accept_button": "Принимаю",
        "privacy_gate_text": (
            "Перед использованием бота ознакомьтесь с политикой "
            "конфиденциальности и примите её.\n\n"
            "Коротко: мы не ведём логи вашего трафика и не храним, какие "
            "сайты вы посещаете. Полный текст — по кнопке ниже."
        ),
        "privacy_accepted_msg": "Спасибо! Политика принята. Можно пользоваться ботом.",
        "privacy_required": "Сначала примите политику конфиденциальности (/start).",
        # Legal-acceptance gate (Privacy Policy + Terms of Service).
        "doc_privacy_label": "Политика конфиденциальности",
        "doc_tos_label": "Пользовательское соглашение",
        "terms_gate_greeting": "Здравствуйте, {name}! Это бот AegisVPN.",
        "terms_gate_intro": "Чтобы продолжить, ознакомьтесь и примите наши документы:",
        "terms_accept_button": "Принять",
        "terms_accepted_msg": "Спасибо! Документы приняты. Можно пользоваться ботом.",
        "terms_required": "Сначала примите наши документы (/start).",
        # /info command.
        "info_about": (
            "AegisVPN — быстрый VPN на протоколах VLESS+REALITY (ядро xray) и "
            "Hysteria2, управляемый прямо через этого бота. Подписка, оплата, "
            "получение ключей и поддержка — всё в одном месте."
        ),
        "info_github_label": "Исходный код на GitHub",
        "info_news_button": "Новости",
        "info_support_button": "Поддержка",
        "buy_stars": "Купить звёзды",
        "back": "Назад",
        "access_unavailable": "Доступ недоступен.",
        "no_active_subscription": "У вас нет активной подписки. Хотите оформить доступ?",
        "subscription_active_title": "Ваша подписка активна",
        "days_left": "Осталось дней: {days}",
        "expires_at": "Истекает: {expires_at}",
        "subscription_lifetime": "Срок действия: не ограничен",
        "subscription_link": "Ссылка на подписку:\n{link}",
        "subscription_link_safe": "Безопасные локации:\n{link}",
        "subscription_link_fast": "Быстрые локации:\n{link}",
        "subscription_links_unavailable": "Сейчас для вашей подписки нет доступных локаций.",
        "subscription_hint": "Выберите нужный формат подписки и откройте инструкцию внутри него.",
        "subscription_choose_format": "Выберите формат подписки кнопками ниже.",
        "v2ray_title": "Подписка",
        "back_to_subscription": "Назад в подписку",
        "v2ray_detail_hint": "Вставьте эту ссылку как подписку в приложение (Happ, Hiddify, v2rayTun и др.).",
        "start_text": (
            "Привет, {name}! Я Aegis VPN бот.\n\n"
            "Основные команды:\n"
            "/subscription - ваша подписка\n"
            "/help - помощь\n"
            "/settings - настройки"
        ),
        "help_text": (
            "Доступные команды:\n"
            "/start - запустить бота\n"
            "/help - помощь\n"
            "/subscription - ваша подписка\n"
            "/settings - настройки\n"
        ),
        "settings_title": "Настройки",
        "settings_language": "Язык: {language_name}",
        "language_russian": "Русский",
        "language_english": "English",
        "change_language": "Сменить язык",
        # Per-location transport settings ("Локации").
        "locations_btn": "Локации",
        "locations_title": "Локации",
        "locations_intro": "Выберите локацию, чтобы настроить протокол и транспорт.",
        "locations_none": "Сейчас для вашей подписки нет доступных локаций.",
        "location_no_alt": (
            "Для этой локации доступен только стандартный транспорт. "
            "Настройка появится позже."
        ),
        "location_settings_title": "Настройки локации: {name}",
        "location_protocol_label": "Протокол",
        "location_transport_label": "Тип транспорта",
        "location_protocol_value_btn": "Протокол: {value}",
        "location_transport_value_btn": "Транспорт: {value}",
        "location_proto_vless": "VLESS",
        "location_proto_hy2": "Hysteria2",
        "location_transport_xhttp": "XHTTP",
        "location_transport_tcp": "TCP + VISION",
        "location_current_mark": " [текущий]",
        "location_selected_mark": " (текущий)",
        "location_reset_btn": "Сбросить (по умолчанию)",
        "location_saved": "Сохранено",
        "location_hy2_unavailable": "Hysteria2 пока недоступен.",
        "back_to_locations": "Назад к локациям",
        "back_to_settings": "Назад к настройкам",
        "delete_account": "Удалить аккаунт",
        "delete_account_warning": (
            "Удалить аккаунт?\n\n"
            "Будут безвозвратно удалены ваши данные и активные подписки, "
            "доступ к VPN отключится на всех серверах. Это действие необратимо."
        ),
        "delete_account_confirm": "Да, удалить аккаунт",
        "delete_account_done": "Аккаунт удалён. Чтобы пользоваться ботом снова, отправьте /start.",
        "language_select": "Выберите язык интерфейса.",
        "language_updated": "Язык обновлён.",
        "setup_text": ("Откройте /subscription, выберите нужный формат подписки и используйте инструкцию внутри него."),
        "setup_text_v2ray": (
            "Как подключиться:\n\n"
            "1. Установите приложение: Happ, Hiddify, v2rayTun, v2rayNG или другое совместимое.\n\n"
            "2. Откройте /subscription и скопируйте ссылку на подписку.\n\n"
            "3. В приложении выберите «Добавить подписку» (import by URL) и вставьте ссылку.\n\n"
            "4. Импортируйте локации и подключайтесь.\n\n"
            "5. Если локации не появились сразу, обновите подписку внутри приложения."
        ),
        "no_plans_available": "Сейчас нет доступных тарифов.",
        "choose_plan_buy": "Выберите тариф для покупки:",
        "choose_plan_renew": "Выберите тариф для продления:",
        "invoice_title_buy": "Покупка Aegis VPN",
        "invoice_title_renew": "Продление Aegis VPN",
        "invoice_desc_buy": "Доступ к VPN на {days} дней",
        "invoice_desc_renew": "Продление доступа к VPN на {days} дней",
        "price_label_buy": "VPN на {days} дней",
        "price_label_renew": "Продление VPN на {days} дней",
        "plan_unavailable": "Тариф недоступен",
        "payment_success": (
            "Оплата прошла успешно.\n\n"
            "Ваша подписка на {days} дней {action_text}.\n\n"
            "Откройте /subscription, чтобы получить ссылку на подключение."
        ),
        "payment_action_activated": "активирована",
        "payment_action_renewed": "продлена",
        "expired_notice": "Ваша подписка истекла и была отключена. Продлите её, чтобы снова пользоваться VPN.",
        "remind_3d": "Ваша подписка истекает через 3 дня. Продлите её заранее, чтобы не потерять доступ.",
        "remind_1d": "Ваша подписка истекает через 1 день. Продлите её сейчас, чтобы доступ не прервался.",
        "trial_started": "Пробный период на 3 дня активирован.",
        "trial_already_used": "Пробный период уже был использован.",
        "trial_active_subscription_exists": "Сначала дождитесь окончания текущей подписки или продлите её.",
        "trial_activation_failed": "Не удалось активировать пробный период. Попробуйте ещё раз.",
        "reissue_subscription": "Перевыпустить подписку",
        "reissue_subscription_warning": (
            "Перевыпустить подписку?\n\n"
            "Будет сгенерирована новая ссылка и новый UUID клиента. "
            "Старая ссылка и все добавленные подключения перестанут работать."
        ),
        "reissue_subscription_confirm_btn": "Да, перевыпустить",
        "reissue_subscription_success": "Подписка перевыпущена.\n\nНовая ссылка:\n{link}\n\nОбновите её в вашем клиенте.",
        "reissue_subscription_no_active": "У вас нет активной подписки для перевыпуска.",
        "reissue_subscription_failed": "Не удалось перевыпустить подписку. Попробуйте ещё раз.",
        "tg_proxy_btn": "Telegram-прокси",
        "vpn_sub_btn": "VPN-подписка",
        "tg_proxy_title": "Telegram MTProxy",
        "tg_proxy_hint": (
            "Прокси для Telegram — работает без VPN-клиента, прямо в настройках Telegram.\n\n"
            "Нажмите на ссылку нужной локации, чтобы добавить прокси:"
        ),
        "tg_proxy_none": "Прокси пока не настроены. Скоро появятся.",
        "devices_btn": "Устройства ({count})",
        "devices_title": "Устройства ({count})",
        "devices_empty": "Нет активных устройств.",
        "devices_hint": "Каждое устройство получает отдельный UUID. До 5 одновременных подключений на подписку.",
        "devices_active_now": "только что",
        "devices_active_min": "{n} мин назад",
        "devices_active_hours": "{n} ч назад",
        "devices_active_days": "{n} д назад",
        "devices_active_never": "нет данных",
        "devices_remove_btn": "Удалить {name}",
        "devices_remove_confirm": "Удалить устройство {name}?\n\nОно будет отключено на всех серверах.",
        "devices_remove_yes": "Да, удалить",
        "devices_removed": "Устройство удалено.",
        "devices_not_found": "Устройство не найдено.",
        "devices_back": "Назад к устройствам",
        "device_detail_added": "Добавлено: {date}",
        "device_detail_os": "Система: {os}",
        "device_detail_build": "Сборка: {build}",
        "device_detail_location": "Откуда добавлено: {location}",
        "device_detail_connected": "Подключено: {location}",
        "device_detail_not_connected": "Не подключено",
        "device_detail_suspended": "Приостановлено",
        "device_detail_traffic": "Трафик: {up} / {down}",
        "device_suspend_btn": "Приостановить",
        "device_resume_btn": "Восстановить",
        "device_remove_btn": "Удалить устройство",
        "device_suspended": "Устройство приостановлено.",
        "device_resumed": "Устройство восстановлено.",
    },
    "en": {
        "buy_vpn": "Buy VPN",
        "renew_vpn": "Renew VPN",
        "trial_vpn": "3-day trial",
        "setup_guide": "Instruction",
        "privacy_button": "Privacy policy",
        "privacy_accept_button": "I accept",
        "privacy_gate_text": (
            "Before using the bot, please review and accept the privacy "
            "policy.\n\n"
            "In short: we keep no logs of your traffic and don't store which "
            "sites you visit. Full text is behind the button below."
        ),
        "privacy_accepted_msg": "Thanks! Policy accepted. You can use the bot now.",
        "privacy_required": "Please accept the privacy policy first (/start).",
        # Legal-acceptance gate (Privacy Policy + Terms of Service).
        "doc_privacy_label": "Privacy Policy",
        "doc_tos_label": "Terms of Service",
        "terms_gate_greeting": "Hello, {name}! This is the AegisVPN bot.",
        "terms_gate_intro": "To continue, please review and accept our documents:",
        "terms_accept_button": "Accept",
        "terms_accepted_msg": "Thanks! Documents accepted. You can use the bot now.",
        "terms_required": "Please accept our documents first (/start).",
        # /info command.
        "info_about": (
            "AegisVPN is a fast VPN over VLESS+REALITY (xray core) and "
            "Hysteria2, managed right from this bot. Subscription, payment, "
            "key delivery, and support are all in one place."
        ),
        "info_github_label": "Source code on GitHub",
        "info_news_button": "News",
        "info_support_button": "Support",
        "buy_stars": "Buy Stars",
        "back": "Back",
        "access_unavailable": "Access unavailable.",
        "no_active_subscription": "You do not have an active subscription yet. Want to get one?",
        "subscription_active_title": "Your subscription is active",
        "days_left": "Days left: {days}",
        "expires_at": "Expires: {expires_at}",
        "subscription_lifetime": "Duration: unlimited",
        "subscription_link": "Subscription link:\n{link}",
        "subscription_link_safe": "Secure locations:\n{link}",
        "subscription_link_fast": "Fast locations:\n{link}",
        "subscription_links_unavailable": "There are no available locations for your subscription right now.",
        "subscription_hint": "Choose the subscription format you need and open the instruction inside it.",
        "subscription_choose_format": "Choose the subscription format with the buttons below.",
        "v2ray_title": "Subscription",
        "back_to_subscription": "Back to subscription",
        "v2ray_detail_hint": "Paste this link as a subscription into your app (Happ, Hiddify, v2rayTun, etc.).",
        "start_text": (
            "Hello, {name}! I am the Aegis VPN bot.\n\n"
            "Main commands:\n"
            "/subscription - your subscription\n"
            "/help - help\n"
            "/settings - settings"
        ),
        "help_text": (
            "Available commands:\n"
            "/start - start the bot\n"
            "/help - help\n"
            "/subscription - your subscription\n"
            "/settings - settings\n"
        ),
        "settings_title": "Settings",
        "settings_language": "Language: {language_name}",
        "language_russian": "Russian",
        "language_english": "English",
        "change_language": "Change language",
        # Per-location transport settings ("Locations").
        "locations_btn": "Locations",
        "locations_title": "Locations",
        "locations_intro": "Pick a location to configure its protocol and transport.",
        "locations_none": "There are currently no locations available for your subscription.",
        "location_no_alt": (
            "This location only offers the standard transport. "
            "Configuration will be available later."
        ),
        "location_settings_title": "Location settings: {name}",
        "location_protocol_label": "Protocol",
        "location_transport_label": "Transport type",
        "location_protocol_value_btn": "Protocol: {value}",
        "location_transport_value_btn": "Transport: {value}",
        "location_proto_vless": "VLESS",
        "location_proto_hy2": "Hysteria2",
        "location_transport_xhttp": "XHTTP",
        "location_transport_tcp": "TCP + VISION",
        "location_current_mark": " [текущий]",
        "location_selected_mark": " (current)",
        "location_reset_btn": "Reset (default)",
        "location_saved": "Saved",
        "location_hy2_unavailable": "Hysteria2 is not available yet.",
        "back_to_locations": "Back to locations",
        "back_to_settings": "Back to settings",
        "delete_account": "Delete account",
        "delete_account_warning": (
            "Delete your account?\n\n"
            "Your data and active subscriptions will be permanently removed and "
            "VPN access will be revoked on all servers. This cannot be undone."
        ),
        "delete_account_confirm": "Yes, delete my account",
        "delete_account_done": "Account deleted. Send /start to use the bot again.",
        "language_select": "Choose your interface language.",
        "language_updated": "Language updated.",
        "setup_text": (
            "Open /subscription, choose the subscription format you need, and use the instruction inside it."
        ),
        "setup_text_v2ray": (
            "How to connect:\n\n"
            "1. Install an app: Happ, Hiddify, v2rayTun, v2rayNG, or another compatible client.\n\n"
            "2. Open /subscription and copy your subscription link.\n\n"
            "3. In the app, choose \"Add subscription\" (import by URL) and paste the link.\n\n"
            "4. Import the locations and connect.\n\n"
            "5. If the locations don't appear right away, refresh the subscription inside the app."
        ),
        "no_plans_available": "There are no available plans right now.",
        "choose_plan_buy": "Choose a plan to buy:",
        "choose_plan_renew": "Choose a plan to renew:",
        "invoice_title_buy": "Buy Aegis VPN",
        "invoice_title_renew": "Renew Aegis VPN",
        "invoice_desc_buy": "VPN access for {days} days",
        "invoice_desc_renew": "Extend VPN access for {days} days",
        "price_label_buy": "VPN for {days} days",
        "price_label_renew": "VPN renewal for {days} days",
        "plan_unavailable": "Plan unavailable",
        "payment_success": (
            "Payment successful.\n\n"
            "Your {days}-day subscription has been {action_text}.\n\n"
            "Open /subscription to get your connection link."
        ),
        "payment_action_activated": "activated",
        "payment_action_renewed": "renewed",
        "expired_notice": "Your subscription has expired and was disabled. Renew it to use the VPN again.",
        "remind_3d": "Your subscription expires in 3 days. Renew it early so you do not lose access.",
        "remind_1d": "Your subscription expires in 1 day. Renew it now so access is not interrupted.",
        "trial_started": "Your 3-day trial has been activated.",
        "trial_already_used": "Your trial has already been used.",
        "trial_active_subscription_exists": "Wait until the current subscription ends or renew it instead.",
        "trial_activation_failed": "Failed to activate the trial. Please try again.",
        "reissue_subscription": "Reissue subscription",
        "reissue_subscription_warning": (
            "Reissue subscription?\n\n"
            "A new link and client UUID will be generated. "
            "The old link and all added connections will stop working."
        ),
        "reissue_subscription_confirm_btn": "Yes, reissue",
        "reissue_subscription_success": "Subscription reissued.\n\nNew link:\n{link}\n\nUpdate it in your client.",
        "reissue_subscription_no_active": "You do not have an active subscription to reissue.",
        "reissue_subscription_failed": "Failed to reissue the subscription. Please try again.",
        "tg_proxy_btn": "Telegram proxy",
        "vpn_sub_btn": "VPN subscription",
        "tg_proxy_title": "Telegram MTProxy",
        "tg_proxy_hint": (
            "A proxy for Telegram — works without a VPN client, directly in Telegram settings.\n\n"
            "Tap a location link to add the proxy:"
        ),
        "tg_proxy_none": "No proxies configured yet. Coming soon.",
        "devices_btn": "Devices ({count})",
        "devices_title": "Devices ({count})",
        "devices_empty": "No active devices.",
        "devices_hint": "Each device gets a separate UUID. Up to 5 simultaneous connections per subscription.",
        "devices_active_now": "just now",
        "devices_active_min": "{n} min ago",
        "devices_active_hours": "{n} h ago",
        "devices_active_days": "{n} d ago",
        "devices_active_never": "no data",
        "devices_remove_btn": "Remove {name}",
        "devices_remove_confirm": "Remove device {name}?\n\nIt will be disconnected from all servers.",
        "devices_remove_yes": "Yes, remove",
        "devices_removed": "Device removed.",
        "devices_not_found": "Device not found.",
        "devices_back": "Back to devices",
        "device_detail_added": "Added: {date}",
        "device_detail_os": "System: {os}",
        "device_detail_build": "Build: {build}",
        "device_detail_location": "Added from: {location}",
        "device_detail_connected": "Connected: {location}",
        "device_detail_not_connected": "Not connected",
        "device_detail_suspended": "Suspended",
        "device_detail_traffic": "Traffic: {up} / {down}",
        "device_suspend_btn": "Suspend",
        "device_resume_btn": "Resume",
        "device_remove_btn": "Remove device",
        "device_suspended": "Device suspended.",
        "device_resumed": "Device resumed.",
    },
}


def normalize_language(language: str | None) -> str:
    if language in SUPPORTED_LANGUAGES:
        return language
    return "ru"


def t(language: str | None, key: str, **kwargs: object) -> str:
    lang = normalize_language(language)
    template = TEXTS.get(lang, TEXTS["ru"]).get(key, key)
    return template.format(**kwargs)


def language_label(language: str | None) -> str:
    lang = normalize_language(language)
    return t(lang, "language_russian") if lang == "ru" else t(lang, "language_english")


async def get_user_language(tg_id: int) -> str:
    async with async_session_maker() as session:
        result = await session.execute(select(User.language).where(User.tg_id == tg_id))
        language = result.scalar_one_or_none()
    return normalize_language(language)


async def set_user_language(tg_id: int, language: str) -> str | None:
    normalized = normalize_language(language)
    async with async_session_maker() as session:
        result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(tg_id=tg_id, username=None, referrer_id=None, language=normalized)
            session.add(user)
        user.language = normalized
        await session.commit()
        return normalized


def code_block(value: str) -> str:
    return html.code(value)
