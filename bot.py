from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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
            [InlineKeyboardButton("💸 Contas a pagar", callback_data="choose:pay")],
            [InlineKeyboardButton("💰 Contas a receber", callback_data="choose:recv")],
            [InlineKeyboardButton("📊 Fluxo líquido", callback_data="choose:flow")],
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
        "Consulte títulos em aberto/parciais por vencimento e o fluxo líquido projetado.\n"
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


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return

    query = update.callback_query
    assert query is not None
    await query.answer()
    data = query.data or ""

    if data == "menu":
        await query.edit_message_text("Financeiro Bling — escolha uma opção:", reply_markup=menu_keyboard())
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

        settings: Settings = context.application.bot_data["settings"]
        tz: ZoneInfo = context.application.bot_data["timezone"]
        del settings  # settings kept in bot_data for access control; timezone is used below.
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
        text = (
            "🔐 O Bling precisa ser autenticado novamente.\n"
            "Execute `python oauth_setup.py` no terminal do container e tente de novo."
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

    keyboard = period_keyboard(kind) if kind in REPORT_LABELS else menu_keyboard()

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
            "Tokens do Bling não encontrados. O bot iniciará, mas os relatórios "
            "exigirão a execução de oauth_setup.py."
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
    application.add_handler(CommandHandler("fluxo", fluxo_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_error_handler(error_handler)

    logger.info("Iniciando bot Telegram em polling.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
