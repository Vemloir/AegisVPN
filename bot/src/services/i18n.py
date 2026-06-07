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
        "v2ray_title": "Подписка для V2Ray",
        "amnezia_title": "Подписка для AmneziaVPN",
        "back_to_subscription": "Назад в подписку",
        "v2ray_detail_hint": "Вставьте эту ссылку в V2Ray-клиент как подписку.",
        "amnezia_detail_hint": "Скопируйте этот текстовый ключ и вставьте его в AmneziaVPN.",
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
            "Инструкция для V2Ray:\n\n"
            "1. Установите V2Ray-клиент: Happ, Hiddify, v2rayTun, v2rayNG или другой совместимый.\n\n"
            "2. Откройте /subscription и нажмите кнопку V2Ray.\n\n"
            "3. Скопируйте ссылку подписки с экрана V2Ray.\n\n"
            "4. В клиенте выберите добавление подписки по ссылке или import by URL.\n\n"
            "5. Вставьте ссылку и импортируйте локации.\n\n"
            "6. Если локации не появились сразу, обновите подписку внутри клиента."
        ),
        "setup_text_amnezia": (
            "Инструкция для AmneziaVPN:\n\n"
            "1. Установите AmneziaVPN.\n\n"
            "2. Откройте /subscription и нажмите кнопку AmneziaVPN.\n\n"
            "3. Скопируйте весь текстовый ключ с экрана AmneziaVPN.\n\n"
            "4. В приложении нажмите плюс и выберите вставку ключа.\n\n"
            "5. Вставьте скопированный конфиг, затем нажмите Continue и Connect.\n\n"
            "6. Если приложение просит ключ, используйте именно текст из бота, а не HTTPS-ссылку."
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
        "device_detail_last": "Последняя активность: {time}",
        "device_detail_status_active": "Статус: активно",
        "device_detail_status_suspended": "Статус: приостановлено",
        "device_detail_locations": "Локации: {names}",
        "device_detail_no_locations": "Локации: нет доступных",
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
        "v2ray_title": "V2Ray Subscription",
        "amnezia_title": "AmneziaVPN Subscription",
        "back_to_subscription": "Back to subscription",
        "v2ray_detail_hint": "Paste this link into your V2Ray client as a subscription.",
        "amnezia_detail_hint": "Copy this text key and paste it into AmneziaVPN.",
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
            "V2Ray setup guide:\n\n"
            "1. Install a V2Ray client such as Happ, Hiddify, v2rayTun, v2rayNG, or another compatible app.\n\n"
            "2. Open /subscription and press the V2Ray button.\n\n"
            "3. Copy the subscription URL from the V2Ray screen.\n\n"
            "4. In the client, choose add subscription or import by URL.\n\n"
            "5. Paste the link and import the locations.\n\n"
            "6. If the locations do not appear immediately, refresh the subscription inside the client."
        ),
        "setup_text_amnezia": (
            "AmneziaVPN setup guide:\n\n"
            "1. Install AmneziaVPN.\n\n"
            "2. Open /subscription and press the AmneziaVPN button.\n\n"
            "3. Copy the full text key from the AmneziaVPN screen.\n\n"
            "4. In AmneziaVPN tap plus and choose key paste.\n\n"
            "5. Paste the copied config, then tap Continue and Connect.\n\n"
            "6. If the app asks for a key, use the text from the bot, not the HTTPS link."
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
        "device_detail_last": "Last active: {time}",
        "device_detail_status_active": "Status: active",
        "device_detail_status_suspended": "Status: suspended",
        "device_detail_locations": "Locations: {names}",
        "device_detail_no_locations": "Locations: none available",
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
