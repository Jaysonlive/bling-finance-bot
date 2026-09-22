from __future__ import annotations

import calendar
import logging
import secrets
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bling import BlingAPIError, BlingAuthError, BlingClient
from config import ConfigError, Settings

logger = logging.getLogger(__name__)

PERIOD_LABELS = {
    "today": "Hoje",
    "week": "Esta semana",
    "month": "Este mês",
    "year": "Este ano",
}

REPORT_LABELS = {
    "pay": "Contas a pagar",
    "recv": "Contas a receber",
    "flow": "Fluxo de caixa líquido",
}

AUTH_STATE_KEY = "bling_oauth_state"
AUTH_STARTED_KEY = "bling_oauth_started_at"
AUTH_URL_KEY = "bling_oauth_url"
AUTH_FLOW_TTL_SECONDS = 15 * 60


def format_brl(value: Decimal) -> str:
    value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    negative = value < 0
    value = abs(value)
    raw = f"{value:,.2f}"
    raw = raw.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{'-' if negative else ''}R$ {raw}"


def get_period(period: str, tz: ZoneInfo) -> tuple[date, date]:
    today = datetime.now(tz).date()

    if period == "today":
        return today, today
    if period == "week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if period == "month":
        last_day = calendar.monthrange(today.year, today.month)[1]
        return date(today.year, today.month, 1), date(today.year, today.month, last_day)
    if period == "year":
        return date(today.year, 1, 1), date(today.year, 12, 31)

    raise ValueError(f"Período inválido: {period}")


def format_period(start: date, end: date) -> str:
    if start == end:
        return start.strftime("%d/%m/%Y")
    return f"{start.strftime('%d/%m/%Y')} a {end.strftime('%d/%m/%Y')}"


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Saldos — Caixas e Bancos", callback_data="cash:menu")],
            [InlineKeyboardButton("💼 Posição financeira", callback_data="position:menu")],
            [InlineKeyboardButton("💸 Contas a pagar", callback_data="choose:pay")],
            [InlineKeyboardButton("💰 Contas a receber", callback_data="choose:recv")],
            [InlineKeyboardButton("📊 Fluxo líquido", callback_data="choose:flow")],
            [InlineKeyboardButton("🔗 Bling / Conexão", callback_data="bling:menu")],
        ]
    )


def period_keyboard(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Hoje", callback_data=f"report:{kind}:today"),
                InlineKeyboardButton("Semana", callback_data=f"report:{kind}:week"),
            ],
            [
                InlineKeyboardButton("Mês", callback_data=f"report:{kind}:month"),
                InlineKeyboardButton("Ano", callback_data=f"report:{kind}:year"),
            ],
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
        ]
    )


def cash_summary_keyboard(accounts: tuple) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for account in accounts[:25]:
        label = account.description
        if len(label) > 42:
            label = label[:39] + "..."
        rows.append(
            [InlineKeyboardButton(f"🏦 {label}", callback_data=f"cash:account:{account.account_id}")]
        )
    rows.extend(
        [
            [InlineKeyboardButton("🔄 Atualizar saldos", callback_data="cash:refresh")],
            [InlineKeyboardButton("💼 Posição financeira", callback_data="position:menu")],
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def cash_account_keyboard(account_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Atualizar conta", callback_data=f"cash:accountrefresh:{account_id}"
                )
            ],
            [InlineKeyboardButton("💳 Todos os saldos", callback_data="cash:menu")],
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
        ]
    )


def position_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Até fim do mês", callback_data="position:month"),
                InlineKeyboardButton("Até fim do ano", callback_data="position:year"),
            ],
            [InlineKeyboardButton("💳 Ver saldos", callback_data="cash:menu")],
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
        ]
    )


def bling_connection_keyboard(connected: bool) -> InlineKeyboardMarkup:
    label = "🔄 Reautorizar Bling" if connected else "🔐 Autorizar Bling"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data="bling:authorize")],
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
        ]
    )


def authorization_keyboard(authorize_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔐 Abrir autorização do Bling", url=authorize_url)],
            [InlineKeyboardButton("❌ Cancelar", callback_data="bling:cancel")],
        ]
    )


def auth_required_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔐 Autorizar Bling", callback_data="bling:authorize")],
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
        ]
    )


def _clear_pending_oauth(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(AUTH_STATE_KEY, None)
    context.user_data.pop(AUTH_STARTED_KEY, None)
    context.user_data.pop(AUTH_URL_KEY, None)


def _pending_oauth_is_valid(context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = context.user_data.get(AUTH_STATE_KEY)
    started = context.user_data.get(AUTH_STARTED_KEY)
    if not state or not isinstance(started, (int, float)):
        return False
    if time.monotonic() - started > AUTH_FLOW_TTL_SECONDS:
        _clear_pending_oauth(context)
        return False
    return True


def extract_code_and_state(callback_url: str) -> tuple[str, str]:
    """Extract the OAuth authorization code and state from the pasted callback URL."""
    value = callback_url.strip()
    if not value:
        raise ValueError("A URL está vazia.")

    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise ValueError("A URL informada é inválida.") from exc

    if not parsed.scheme or not parsed.query:
        raise ValueError(
            "Cole a URL completa que apareceu no navegador depois de autorizar o Bling."
        )

    params = parse_qs(parsed.query)
    code = (params.get("code") or [""])[0].strip()
    state = (params.get("state") or [""])[0].strip()

    if not code:
        error = (params.get("error") or [""])[0].strip()
        error_description = (params.get("error_description") or [""])[0].strip()
        if error:
            detail = f": {error_description}" if error_description else ""
            raise ValueError(f"O Bling não retornou autorização ({error}{detail}).")
        raise ValueError("A URL não contém o parâmetro 'code'.")
    if not state:
        raise ValueError("A URL não contém o parâmetro 'state'. Gere um novo link pelo bot.")

    return code, state


async def ensure_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    settings: Settings = context.application.bot_data["settings"]

    if user and user.id in settings.allowed_users:
        return True

    user_id = user.id if user else None
    username = user.username if user else None
    logger.warning("Acesso negado ao Telegram: user_id=%s username=%s", user_id, username)

    if update.callback_query:
        await update.callback_query.answer("⛔ Acesso negado.", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("⛔ Acesso negado.")
    return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return

    await update.effective_message.reply_text(
        "Financeiro Bling\n\n"
        "Consulte saldos registrados em Caixas e Bancos, títulos pendentes e projeções financeiras.\n"
        "Escolha uma opção:",
        reply_markup=menu_keyboard(),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_command(update, context)


async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    await update.effective_message.reply_text(
        "💸 Contas a pagar — escolha o período:", reply_markup=period_keyboard("pay")
    )


async def receive_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    await update.effective_message.reply_text(
        "💰 Contas a receber — escolha o período:", reply_markup=period_keyboard("recv")
    )


async def saldos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    await update.effective_chat.send_action(ChatAction.TYPING)
    await _send_cash_summary(update, context, edit=False, force_refresh=False)


async def posicao_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "💼 Posição financeira\n\n"
            "Combina o saldo registrado em Caixas e Bancos com contas a receber e a pagar.\n"
            "Escolha um horizonte ou use:\n"
            "/posicao YYYY-MM-DD YYYY-MM-DD",
            reply_markup=position_keyboard(),
        )
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "Uso correto: /posicao YYYY-MM-DD YYYY-MM-DD\n"
            "Exemplo: /posicao 2026-09-22 2026-12-31"
        )
        return

    try:
        start = date.fromisoformat(context.args[0])
        end = date.fromisoformat(context.args[1])
    except ValueError:
        await update.effective_message.reply_text(
            "Data inválida. Use exatamente o formato YYYY-MM-DD."
        )
        return

    if end < start:
        await update.effective_message.reply_text(
            "A data final não pode ser anterior à data inicial."
        )
        return

    await update.effective_chat.send_action(ChatAction.TYPING)
    await _send_financial_position(update, context, start, end, edit=False)


async def fluxo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "📊 Fluxo líquido — escolha um período ou use:\n"
            "/fluxo YYYY-MM-DD YYYY-MM-DD",
            reply_markup=period_keyboard("flow"),
        )
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "Uso correto: /fluxo YYYY-MM-DD YYYY-MM-DD\n"
            "Exemplo: /fluxo 2026-09-01 2026-09-30"
        )
        return

    try:
        start = date.fromisoformat(context.args[0])
        end = date.fromisoformat(context.args[1])
    except ValueError:
        await update.effective_message.reply_text(
            "Data inválida. Use exatamente o formato YYYY-MM-DD."
        )
        return

    if end < start:
        await update.effective_message.reply_text(
            "A data final não pode ser anterior à data inicial."
        )
        return

    await update.effective_chat.send_action(ChatAction.TYPING)
    await _send_report(update, context, "flow", start, end, edit=False)


async def _bling_status(context: ContextTypes.DEFAULT_TYPE) -> tuple[bool, str]:
    bling: BlingClient = context.application.bot_data["bling"]
    try:
        loaded = await bling.load_tokens()
    except BlingAuthError as exc:
        logger.warning("Tokens do Bling inválidos ao consultar status: %s", exc)
        return False, "🔴 Tokens ausentes ou inválidos"

    if loaded:
        return True, "🟢 Conectado"
    return False, "🔴 Não autenticado"


async def _send_bling_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool,
) -> None:
    connected, status = await _bling_status(context)
    text = (
        "🔗 Integração Bling\n\n"
        f"Status: {status}\n"
        f"Renovação automática: {'✅ Ativa' if connected else '—'}\n"
        f"Tokens persistentes: {'✅ Configurados' if connected else '—'}\n\n"
        "Quando conectado, o bot renova os tokens automaticamente."
    )
    keyboard = bling_connection_keyboard(connected)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def bling_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    await _send_bling_status(update, context, edit=False)


async def _begin_bling_authorization(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool,
) -> None:
    bling: BlingClient = context.application.bot_data["bling"]
    state = secrets.token_urlsafe(24)
    authorize_url = bling.build_authorize_url(state)

    context.user_data[AUTH_STATE_KEY] = state
    context.user_data[AUTH_STARTED_KEY] = time.monotonic()
    context.user_data[AUTH_URL_KEY] = authorize_url

    text = (
        "🔐 Autorização do Bling\n\n"
        "1. Toque em “Abrir autorização do Bling”.\n"
        "2. Entre no Bling e autorize o aplicativo.\n"
        "3. Depois do redirecionamento, copie a URL COMPLETA da barra do navegador.\n"
        "4. Volte aqui e cole essa URL como uma mensagem.\n\n"
        "⏱️ Depois que o Bling gerar o código, cole a URL imediatamente, pois o código "
        "de autorização expira rapidamente.\n\n"
        "🔒 O bot confere o parâmetro state antes de aceitar o código."
    )
    keyboard = authorization_keyboard(authorize_url)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def authorize_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    await _begin_bling_authorization(update, context, edit=False)


async def oauth_callback_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the callback URL pasted by an allowed user after Bling authorization."""
    if not await ensure_allowed(update, context):
        return

    if not _pending_oauth_is_valid(context):
        # Ordinary text outside an OAuth flow is ignored; the bot is command/menu driven.
        return

    message = update.effective_message
    if message is None or not message.text:
        return

    expected_state = str(context.user_data[AUTH_STATE_KEY])

    try:
        code, returned_state = extract_code_and_state(message.text)
    except ValueError as exc:
        await message.reply_text(
            f"⚠️ {exc}\n\n"
            "Cole a URL completa exibida na barra do navegador depois da autorização.",
            reply_markup=authorization_keyboard(str(context.user_data[AUTH_URL_KEY])),
        )
        return

    if not secrets.compare_digest(returned_state, expected_state):
        logger.warning(
            "OAuth state inválido recebido via Telegram: user_id=%s",
            update.effective_user.id if update.effective_user else None,
        )
        await message.reply_text(
            "⛔ O state dessa URL não corresponde ao link gerado pelo bot.\n"
            "Por segurança, ela não foi utilizada. Gere um novo link com /autorizar."
        )
        _clear_pending_oauth(context)
        return

    bling: BlingClient = context.application.bot_data["bling"]
    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        await bling.bootstrap_authorization_code(code)
    except BlingAuthError as exc:
        logger.warning("Falha ao concluir OAuth do Bling via Telegram: %s", exc)
        _clear_pending_oauth(context)

        detail = str(exc).lower()
        if "expired" in detail or "invalid_grant" in detail:
            text = (
                "⌛ O código de autorização expirou ou já foi utilizado.\n\n"
                "Gere um novo link e faça novamente; depois de autorizar, cole a URL aqui imediatamente."
            )
        else:
            text = (
                "⚠️ Não foi possível concluir a autenticação do Bling.\n"
                f"Detalhe: {str(exc)[:500]}"
            )
        await message.reply_text(text, reply_markup=auth_required_keyboard())
        return
    except Exception:
        logger.exception("Erro inesperado ao concluir OAuth do Bling via Telegram")
        _clear_pending_oauth(context)
        await message.reply_text(
            "⚠️ Ocorreu um erro inesperado durante a autenticação. Tente novamente com /autorizar.",
            reply_markup=auth_required_keyboard(),
        )
        return

    _clear_pending_oauth(context)
    logger.info(
        "Bling autenticado via Telegram com sucesso: user_id=%s",
        update.effective_user.id if update.effective_user else None,
    )
    await message.reply_text(
        "✅ Bling autenticado com sucesso.\n\n"
        "Os tokens foram salvos no volume persistente e a renovação automática está ativa.",
        reply_markup=menu_keyboard(),
    )


def _cash_scope_message(exc: BlingAuthError) -> str:
    detail = str(exc).lower()
    if "403" in detail or "escopo" in detail or "scope" in detail:
        return (
            "🔐 O aplicativo ainda não tem acesso a Caixas e Bancos.\n\n"
            "No cadastro do aplicativo no Bling, habilite o escopo de leitura “Caixas e Bancos” "
            "e depois use /autorizar para reautorizar a conta."
        )
    return (
        "🔐 A conexão com o Bling precisa ser autorizada ou renovada.\n\n"
        "Use /autorizar para conectar novamente."
    )


def _cash_summary_text(summary) -> str:
    lines = ["💳 Saldos — Caixas e Bancos", ""]
    if not summary.accounts:
        lines.append("Nenhum lançamento financeiro foi retornado pelo Bling.")
    else:
        for account in summary.accounts:
            lines.append(f"🏦 {account.description}: {format_brl(account.balance)}")
        lines.extend(
            [
                "",
                f"💵 Total registrado no Bling: {format_brl(summary.total_balance)}",
                "",
                "ℹ️ Saldo calculado pelos lançamentos de Caixas e Bancos. "
                "Não é consulta em tempo real ao internet banking.",
            ]
        )
    return "\n".join(lines)


async def _send_cash_summary(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool,
    force_refresh: bool,
) -> None:
    bling: BlingClient = context.application.bot_data["bling"]
    auth_error = False
    accounts: tuple = ()
    try:
        summary = await bling.get_cash_summary(force_refresh=force_refresh)
        text = _cash_summary_text(summary)
        accounts = summary.accounts
    except BlingAuthError as exc:
        logger.warning("Sem autorização para Caixas e Bancos: %s", exc)
        auth_error = True
        text = _cash_scope_message(exc)
    except BlingAPIError as exc:
        logger.exception("Erro ao consultar Caixas e Bancos")
        text = (
            "⚠️ Não foi possível consultar Caixas e Bancos agora.\n"
            f"Detalhe técnico: {str(exc)[:700]}"
        )
    except Exception:
        logger.exception("Erro inesperado ao consultar saldos de Caixas e Bancos")
        text = "⚠️ Ocorreu um erro inesperado ao consultar os saldos. Consulte os logs."

    keyboard = auth_required_keyboard() if auth_error else cash_summary_keyboard(accounts)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def _send_cash_account(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    account_id: str,
    *,
    force_refresh: bool,
) -> None:
    bling: BlingClient = context.application.bot_data["bling"]
    tz: ZoneInfo = context.application.bot_data["timezone"]
    today = datetime.now(tz).date()
    month_start = date(today.year, today.month, 1)

    try:
        account, movements = await bling.get_cash_account(
            account_id, force_refresh=force_refresh
        )
        month_movements = [
            movement
            for movement in movements
            if movement.movement_date is not None
            and month_start <= movement.movement_date <= today
        ]
        month_credits = sum(
            (m.amount for m in month_movements if m.direction == "C"), Decimal("0")
        )
        month_debits = sum(
            (m.amount for m in month_movements if m.direction == "D"), Decimal("0")
        )
        lines = [
            f"🏦 {account.description}",
            "",
            f"Saldo registrado: {format_brl(account.balance)}",
            "",
            f"Entradas no mês: {format_brl(month_credits)}",
            f"Saídas no mês: {format_brl(month_debits)}",
            f"Movimento líquido no mês: {format_brl(month_credits - month_debits)}",
            f"Lançamentos históricos: {account.movement_count}",
        ]
        recent = list(movements[:8])
        if recent:
            lines.extend(["", "📋 Últimos lançamentos:"])
            for movement in recent:
                when = (
                    movement.movement_date.strftime("%d/%m/%Y")
                    if movement.movement_date
                    else "sem data"
                )
                sign = "+" if movement.direction == "C" else "-"
                desc = movement.description or "Lançamento"
                if len(desc) > 44:
                    desc = desc[:41] + "..."
                lines.append(
                    f"{when} • {sign}{format_brl(movement.amount).replace('R$ ', 'R$ ')} • {desc}"
                )
        text = "\n".join(lines)
        keyboard = cash_account_keyboard(account.account_id)
    except BlingAuthError as exc:
        text = _cash_scope_message(exc)
        keyboard = auth_required_keyboard()
    except BlingAPIError as exc:
        text = f"⚠️ Não foi possível abrir essa conta.\nDetalhe: {str(exc)[:600]}"
        keyboard = cash_summary_keyboard(())

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)


async def _send_financial_position(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    start: date,
    end: date,
    *,
    edit: bool,
) -> None:
    bling: BlingClient = context.application.bot_data["bling"]
    auth_error = False
    try:
        cash = await bling.get_cash_summary()
        flow = await bling.get_financial_summary(start, end)
        projected = cash.total_balance + flow.net
        signal = "🟢" if projected >= 0 else "🔴"
        text = (
            "💼 Posição financeira\n"
            f"📅 Projeção: {format_period(start, end)}\n\n"
            f"💳 Saldo atual no Bling: {format_brl(cash.total_balance)}\n"
            f"💰 A receber no período: {format_brl(flow.receivable.total)} "
            f"({flow.receivable.count} títulos)\n"
            f"💸 A pagar no período: {format_brl(flow.payable.total)} "
            f"({flow.payable.count} títulos)\n"
            f"📊 Movimento futuro líquido: {format_brl(flow.net)}\n\n"
            f"{signal} Saldo projetado: {format_brl(projected)}\n\n"
            "ℹ️ Saldo atual = lançamentos registrados em Caixas e Bancos; "
            "não é saldo bancário em tempo real."
        )
    except BlingAuthError as exc:
        logger.warning("Sem autorização ao calcular posição financeira: %s", exc)
        auth_error = True
        text = _cash_scope_message(exc)
    except BlingAPIError as exc:
        logger.exception("Erro ao calcular posição financeira")
        text = f"⚠️ Não foi possível calcular a posição financeira.\nDetalhe: {str(exc)[:700]}"
    except Exception:
        logger.exception("Erro inesperado ao calcular posição financeira")
        text = "⚠️ Ocorreu um erro inesperado ao calcular a posição financeira."

    keyboard = auth_required_keyboard() if auth_error else position_keyboard()
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return

    query = update.callback_query
    assert query is not None
    await query.answer()
    data = query.data or ""

    if data == "menu":
        await query.edit_message_text(
            "Financeiro Bling — escolha uma opção:", reply_markup=menu_keyboard()
        )
        return

    if data == "cash:menu":
        await query.edit_message_text(
            "⏳ Consultando saldos de Caixas e Bancos...\n"
            "A primeira consulta pode levar alguns segundos."
        )
        await _send_cash_summary(update, context, edit=True, force_refresh=False)
        return

    if data == "cash:refresh":
        await query.edit_message_text("⏳ Atualizando saldos de Caixas e Bancos...")
        await _send_cash_summary(update, context, edit=True, force_refresh=True)
        return

    if data.startswith("cash:accountrefresh:"):
        account_id = data.split(":", 2)[2]
        await query.edit_message_text("⏳ Atualizando conta financeira...")
        await _send_cash_account(
            update, context, account_id, force_refresh=True
        )
        return

    if data.startswith("cash:account:"):
        account_id = data.split(":", 2)[2]
        await query.edit_message_text("⏳ Carregando conta financeira...")
        await _send_cash_account(
            update, context, account_id, force_refresh=False
        )
        return

    if data == "position:menu":
        await query.edit_message_text(
            "💼 Posição financeira\n\n"
            "Escolha até onde deseja projetar o saldo. "
            "Também é possível usar /posicao YYYY-MM-DD YYYY-MM-DD.",
            reply_markup=position_keyboard(),
        )
        return

    if data in {"position:month", "position:year"}:
        tz: ZoneInfo = context.application.bot_data["timezone"]
        today = datetime.now(tz).date()
        if data == "position:month":
            last_day = calendar.monthrange(today.year, today.month)[1]
            end = date(today.year, today.month, last_day)
        else:
            end = date(today.year, 12, 31)
        await query.edit_message_text("⏳ Calculando posição financeira...")
        await _send_financial_position(update, context, today, end, edit=True)
        return

    if data == "bling:menu":
        await _send_bling_status(update, context, edit=True)
        return

    if data == "bling:authorize":
        await _begin_bling_authorization(update, context, edit=True)
        return

    if data == "bling:cancel":
        _clear_pending_oauth(context)
        await query.edit_message_text(
            "Autorização cancelada.\n\nFinanceiro Bling — escolha uma opção:",
            reply_markup=menu_keyboard(),
        )
        return

    if data.startswith("choose:"):
        kind = data.split(":", 1)[1]
        if kind not in REPORT_LABELS:
            await query.edit_message_text("Opção inválida.", reply_markup=menu_keyboard())
            return
        await query.edit_message_text(
            f"{REPORT_LABELS[kind]} — escolha o período:",
            reply_markup=period_keyboard(kind),
        )
        return

    if data.startswith("report:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.edit_message_text("Opção inválida.", reply_markup=menu_keyboard())
            return

        _, kind, period = parts
        if kind not in REPORT_LABELS or period not in PERIOD_LABELS:
            await query.edit_message_text("Opção inválida.", reply_markup=menu_keyboard())
            return

        tz: ZoneInfo = context.application.bot_data["timezone"]
        start, end = get_period(period, tz)

        await query.edit_message_text(
            f"⏳ Consultando {REPORT_LABELS[kind].lower()} — {PERIOD_LABELS[period]}..."
        )
        await _send_report(update, context, kind, start, end, edit=True)
        return

    await query.edit_message_text("Opção inválida.", reply_markup=menu_keyboard())


async def _send_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: str,
    start: date,
    end: date,
    *,
    edit: bool,
) -> None:
    bling: BlingClient = context.application.bot_data["bling"]
    auth_error = False

    try:
        if kind == "pay":
            result = await bling.get_payable_summary(start, end)
            text = (
                "💸 Contas a pagar\n"
                f"📅 {format_period(start, end)}\n\n"
                f"Títulos pendentes: {result.count}\n"
                f"Total a pagar: {format_brl(result.total)}"
            )
        elif kind == "recv":
            result = await bling.get_receivable_summary(start, end)
            text = (
                "💰 Contas a receber\n"
                f"📅 {format_period(start, end)}\n\n"
                f"Títulos pendentes: {result.count}\n"
                f"Total a receber: {format_brl(result.total)}"
            )
        elif kind == "flow":
            result = await bling.get_financial_summary(start, end)
            signal = "🟢" if result.net >= 0 else "🔴"
            text = (
                "📊 Fluxo de caixa líquido\n"
                f"📅 {format_period(start, end)}\n\n"
                f"💰 A receber: {format_brl(result.receivable.total)} "
                f"({result.receivable.count} títulos)\n"
                f"💸 A pagar: {format_brl(result.payable.total)} "
                f"({result.payable.count} títulos)\n"
                f"{signal} Líquido: {format_brl(result.net)}"
            )
        else:
            raise ValueError("Tipo de relatório inválido")

    except BlingAuthError as exc:
        logger.error("Erro de autenticação Bling: %s", exc)
        auth_error = True
        text = (
            "🔐 A conexão com o Bling precisa ser autorizada ou renovada.\n\n"
            "Toque no botão abaixo. Você não precisa entrar no terminal do servidor."
        )
    except BlingAPIError as exc:
        logger.exception("Erro ao consultar API Bling")
        text = (
            "⚠️ Não foi possível consultar o Bling agora.\n"
            f"Detalhe técnico: {str(exc)[:700]}"
        )
    except Exception:
        logger.exception("Erro inesperado ao gerar relatório")
        text = "⚠️ Ocorreu um erro inesperado ao gerar o relatório. Consulte os logs."

    keyboard = auth_required_keyboard() if auth_error else period_keyboard(kind)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Erro não tratado no Telegram", exc_info=context.error)


async def post_init(application: Application) -> None:
    bling: BlingClient = application.bot_data["bling"]
    try:
        loaded = await bling.load_tokens()
    except BlingAuthError as exc:
        logger.error("Arquivo de tokens inválido: %s", exc)
        loaded = False

    if loaded:
        logger.info("Tokens do Bling carregados do volume persistente.")
    else:
        logger.warning(
            "Tokens do Bling não encontrados. O bot iniciará normalmente; "
            "use /autorizar no Telegram para conectar o Bling."
        )

    await application.bot.set_my_commands(
        [
            BotCommand("start", "Abrir menu financeiro"),
            BotCommand("saldos", "Saldos de Caixas e Bancos"),
            BotCommand("posicao", "Saldo atual + projeção financeira"),
            BotCommand("fluxo", "Fluxo por período ou datas livres"),
            BotCommand("pagar", "Contas a pagar"),
            BotCommand("receber", "Contas a receber"),
            BotCommand("autorizar", "Autorizar ou reautorizar o Bling"),
            BotCommand("status_bling", "Ver status da conexão com o Bling"),
            BotCommand("menu", "Abrir menu"),
        ]
    )


async def post_shutdown(application: Application) -> None:
    bling: BlingClient = application.bot_data["bling"]
    await bling.close()


def main() -> None:
    try:
        settings = Settings.from_env()
        timezone = ZoneInfo(settings.timezone)
    except (ConfigError, ZoneInfoNotFoundError) as exc:
        raise SystemExit(f"Erro de configuração: {exc}") from exc

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    bling = BlingClient(
        client_id=settings.bling_client_id,
        client_secret=settings.bling_client_secret,
        token_file=settings.bling_token_file,
    )

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    application.bot_data["timezone"] = timezone
    application.bot_data["bling"] = bling

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("pagar", pay_command))
    application.add_handler(CommandHandler("receber", receive_command))
    application.add_handler(CommandHandler("saldos", saldos_command))
    application.add_handler(CommandHandler("posicao", posicao_command))
    application.add_handler(CommandHandler("fluxo", fluxo_command))
    application.add_handler(CommandHandler("autorizar", authorize_command))
    application.add_handler(CommandHandler("status_bling", bling_status_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, oauth_callback_message)
    )
    application.add_error_handler(error_handler)

    logger.info("Iniciando bot Telegram em polling.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
