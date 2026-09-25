from __future__ import annotations

import asyncio
import calendar
import logging
import secrets
import time
import shlex
import re
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
from services import ReportService
from entry_flow import (
    ENTRY_DRAFT_KEY,
    clear_entry_draft,
    entry_menu_keyboard,
    handle_entry_callback,
    handle_entry_text,
    start_entry_flow,
)
from financial_card_flow import (
    CARD_DRAFT_KEY,
    clear_financial_card_draft,
    detect_financial_card_query,
    financial_card_menu_keyboard,
    handle_financial_card_callback,
    handle_financial_card_text,
    start_financial_card_flow,
    start_financial_card_from_natural_language,
)

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
CALIBRATION_PENDING_KEY = "cash_calibration_pending"

REPORT_MENU = [
    ("1️⃣ Para onde vão R$100", "catdist"),
    ("2️⃣ Ranking categorias", "catrank"),
    ("3️⃣ Ranking fornecedores", "suprank"),
    ("4️⃣ Histórico fornecedor", "suphistory"),
    ("5️⃣ Recorrentes", "recurring"),
    ("6️⃣ Fixas x variáveis", "fixedvar"),
    ("7️⃣ Evolução mensal", "evolution"),
    ("8️⃣ Variação categorias", "variation"),
    ("9️⃣ Pró-labore / sócios", "partners"),
    ("🔟 Administrativas", "admin"),
    ("1️⃣1️⃣ Operacionais", "operational"),
    ("1️⃣2️⃣ DRE gerencial", "dre"),
    ("1️⃣3️⃣ Custo dia/hora", "opex"),
    ("1️⃣4️⃣ Pequenas despesas", "small"),
    ("1️⃣5️⃣ Gastos fora do padrão", "anomaly"),
]


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
            [InlineKeyboardButton("➕ Novo lançamento", callback_data="entry:menu")],
            [InlineKeyboardButton("💳 Saldos — Caixas e Bancos", callback_data="cash:menu")],
            [InlineKeyboardButton("💼 Posição financeira", callback_data="position:menu")],
            [InlineKeyboardButton("💸 Contas a pagar", callback_data="choose:pay")],
            [InlineKeyboardButton("💰 Contas a receber", callback_data="choose:recv")],
            [InlineKeyboardButton("📒 Ficha financeira", callback_data="card:menu")],
            [InlineKeyboardButton("📊 Fluxo líquido", callback_data="choose:flow")],
            [InlineKeyboardButton("📚 Relatórios gerenciais", callback_data="reports:menu")],
            [InlineKeyboardButton("🔄 Sincronização", callback_data="sync:menu")],
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



def reports_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"reports:{code}")] for label, code in REPORT_MENU]
    rows.append([InlineKeyboardButton("⬅️ Menu", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def sync_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Atualizar últimos dias", callback_data="sync:recent")],
        [InlineKeyboardButton("💳 Calibrar saldos", callback_data="sync:calibrate")],
        [InlineKeyboardButton("📋 Status do banco local", callback_data="sync:status")],
        [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
    ])


async def send_long_message(message, text: str, *, reply_markup=None) -> None:
    chunks = []
    remaining = text
    while len(remaining) > 3900:
        cut = remaining.rfind("\n", 0, 3900)
        if cut < 1000:
            cut = 3900
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    chunks.append(remaining)
    for idx, chunk in enumerate(chunks):
        await message.reply_text(chunk, reply_markup=reply_markup if idx == len(chunks)-1 else None)


def _parse_decimal_br(value: str) -> Decimal:
    raw = value.strip().replace("R$", "").replace(" ", "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    return Decimal(raw)


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
    context.user_data.pop(CALIBRATION_PENDING_KEY, None)
    clear_entry_draft(context)
    clear_financial_card_draft(context)

    await update.effective_message.reply_text(
        "Financeiro Bling\n\n"
        "Consulte saldos, títulos, projeções e relatórios ou crie contas e lançamentos de caixa direto pelo Telegram.\n"
        "Escolha uma opção:",
        reply_markup=menu_keyboard(),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_command(update, context)


async def lancar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    context.user_data.pop(CALIBRATION_PENDING_KEY, None)
    clear_entry_draft(context)
    await update.effective_message.reply_text(
        "➕ Lançamentos financeiros\n\n"
        "Escolha o tipo de lançamento. Antes de gravar no Bling, o bot mostra todos os dados para confirmação.",
        reply_markup=entry_menu_keyboard(),
    )


async def nova_pagar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    context.user_data.pop(CALIBRATION_PENDING_KEY, None)
    await start_entry_flow(update, context, "payable")


async def nova_receber_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    context.user_data.pop(CALIBRATION_PENDING_KEY, None)
    await start_entry_flow(update, context, "receivable")


async def caixa_saida_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    context.user_data.pop(CALIBRATION_PENDING_KEY, None)
    await start_entry_flow(update, context, "cash_out")


async def caixa_entrada_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    context.user_data.pop(CALIBRATION_PENDING_KEY, None)
    await start_entry_flow(update, context, "cash_in")


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


async def ficha_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    context.user_data.pop(CALIBRATION_PENDING_KEY, None)
    clear_entry_draft(context)
    await start_financial_card_flow(update, context)


async def ficha_pagar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    context.user_data.pop(CALIBRATION_PENDING_KEY, None)
    clear_entry_draft(context)
    await start_financial_card_flow(update, context, "payable")


async def ficha_receber_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    context.user_data.pop(CALIBRATION_PENDING_KEY, None)
    clear_entry_draft(context)
    await start_financial_card_flow(update, context, "receivable")


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


async def relatorios_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    await update.effective_message.reply_text(
        "📚 Relatórios gerenciais\n\nVocê pode tocar numa opção ou simplesmente escrever uma pergunta, por exemplo:\n"
        "• Quanto gastei com alimentação este mês?\n"
        "• Me mostre os 5 maiores fornecedores dos últimos 12 meses.\n"
        "• Quanto custa um dia da empresa?\n"
        "• Me mande a DRE de 2025.",
        reply_markup=reports_keyboard(),
    )


async def sincronizar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    bling: BlingClient = context.application.bot_data["bling"]
    tz: ZoneInfo = context.application.bot_data["timezone"]
    today = datetime.now(tz).date()
    try:
        if not context.args:
            start, end, count = await bling.sync_cash_recent(today, force=True)
            await update.effective_message.reply_text(
                f"✅ Sincronização incremental concluída.\n📅 {format_period(start,end)}\n"
                f"Registros recebidos do Bling: {count}\n\n"
                "O histórico antigo não foi reconsultado."
            )
            return
        if len(context.args) != 2:
            await update.effective_message.reply_text(
                "Use /sincronizar para atualizar a janela recente ou:\n"
                "/sincronizar YYYY-MM-DD YYYY-MM-DD"
            )
            return
        start = date.fromisoformat(context.args[0])
        end = date.fromisoformat(context.args[1])
        if end < start:
            raise ValueError("A data final não pode ser anterior à inicial.")
        count = await bling.resync_cash_period(start, end)
        await update.effective_message.reply_text(
            f"✅ Período ressincronizado.\n📅 {format_period(start,end)}\n"
            f"Registros gravados/atualizados: {count}\n\n"
            "Somente esse intervalo foi buscado novamente no Bling."
        )
    except ValueError as exc:
        await update.effective_message.reply_text(f"⚠️ {exc}")
    except (BlingAuthError, BlingAPIError) as exc:
        await update.effective_message.reply_text(f"⚠️ Falha ao sincronizar: {str(exc)[:700]}")


async def status_sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    bling: BlingClient = context.application.bot_data["bling"]
    st = await bling.get_cash_sync_status()
    last = st.last_sync_at.astimezone(context.application.bot_data["timezone"]).strftime("%d/%m/%Y %H:%M") if st.last_sync_at else "nunca"
    text = (
        "🗄 Banco financeiro local\n\n"
        f"Lançamentos: {st.movements}\n"
        f"Contas conhecidas: {st.accounts}\n"
        f"Contas ativas: {st.enabled_accounts}\n"
        f"Saldos calibrados: {st.calibrated_accounts}/{st.enabled_accounts}\n"
        f"Categorias: {st.categories}\n"
        f"Cobertura: {st.oldest_date or '—'} até {st.newest_date or '—'}\n"
        f"Última sincronização: {last}\n"
    )
    await update.effective_message.reply_text(text, reply_markup=sync_keyboard())


async def contas_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    bling: BlingClient = context.application.bot_data["bling"]
    accounts = await bling.list_local_cash_accounts(enabled_only=False)
    lines = ["🏦 Contas financeiras no banco local", ""]
    for a in accounts:
        mark = "✅" if a.enabled else "⏸"
        base = f" | base {a.base_date:%d/%m/%Y}" if a.base_date else " | saldo não calibrado"
        lines.append(f"{mark} {a.description} [{a.account_id}]{base}")
    lines += ["", "Para corrigir detecção automática:", "/ativar_conta NOME", "/desativar_conta NOME"]
    await send_long_message(update.effective_message, "\n".join(lines))


async def ativar_conta_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Uso: /ativar_conta nome da conta")
        return
    bling: BlingClient = context.application.bot_data["bling"]
    try:
        a = await bling.set_cash_account_enabled(" ".join(context.args), True)
        await update.effective_message.reply_text(f"✅ Conta ativada: {a.description}")
    except ValueError as exc:
        await update.effective_message.reply_text(f"⚠️ {exc}")


async def desativar_conta_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Uso: /desativar_conta nome da conta")
        return
    bling: BlingClient = context.application.bot_data["bling"]
    try:
        a = await bling.set_cash_account_enabled(" ".join(context.args), False)
        await update.effective_message.reply_text(f"⏸ Conta desativada: {a.description}")
    except ValueError as exc:
        await update.effective_message.reply_text(f"⚠️ {exc}")


async def calibrar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    bling: BlingClient = context.application.bot_data["bling"]
    tz: ZoneInfo = context.application.bot_data["timezone"]
    today = datetime.now(tz).date()
    await bling.sync_cash_recent(today, force=True)
    accounts = await bling.list_local_cash_accounts(enabled_only=True)
    if not accounts:
        await update.effective_message.reply_text("⚠️ Nenhuma conta ativa detectada. Use /contas e ative as contas corretas.")
        return
    context.user_data[CALIBRATION_PENDING_KEY] = True
    names = "; ".join(f"{a.description}=0,00" for a in accounts)
    await update.effective_message.reply_text(
        "💳 Calibração de saldo\n\n"
        "A API pública do Bling não expõe o saldo inicial/atual das contas no catálogo. "
        "Por isso, informe uma única vez os saldos que aparecem hoje no painel do Bling. "
        "Depois o SQLite mantém o saldo por sincronização incremental.\n\n"
        "Responda nesta conversa no formato:\n" + names + "\n\n"
        "Exemplo: Bling Conta=1209,28; Caixa=734,88; Infinity Bank=377,14; Inter=0,74"
    )


async def categorias_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    bling: BlingClient = context.application.bot_data["bling"]
    db = bling.cash_db
    try:
        await bling.sync_categories(force=False)
    except (BlingAuthError, BlingAPIError) as exc:
        logger.warning("Categorias usando cache local: %s", exc)
    rows = await asyncio.to_thread(db.classification_rows)
    lines = ["🏷 Categorias e classificação gerencial", ""]
    for r in rows[:80]:
        lines.append(
            f"• {r['description']} | {r['cost_behavior']} | {r['managerial_group']} | "
            f"DRE={r['dre_line']} | OPEX={'sim' if r['is_opex'] else 'não'}"
        )
    lines += ["", "Editar:", '/classificar "Software" comportamento=fixed grupo=administrative dre=operating_expenses opex=sim']
    await send_long_message(update.effective_message, "\n".join(lines))


async def classificar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    raw = " ".join(context.args).strip()
    if not raw:
        await update.effective_message.reply_text(
            'Uso: /classificar "Categoria" comportamento=fixed grupo=administrative dre=operating_expenses opex=sim\n\n'
            "Comportamento: fixed, variable, direct, administrative, other\n"
            "Grupo: administrative, operational, commercial, marketing, financial, partners, taxes, revenue, other\n"
            "DRE: gross_revenue, deductions, direct_costs, operating_expenses, other_income, other_expenses, ignore, auto"
        )
        return
    try:
        parts = shlex.split(raw)
        category = parts[0]
        opts: dict[str, str] = {}
        for token in parts[1:]:
            if "=" in token:
                k, v = token.split("=", 1)
                opts[k.casefold()] = v.strip()
        allowed_behavior = {"fixed", "variable", "direct", "administrative", "other"}
        allowed_group = {"administrative", "operational", "commercial", "marketing", "financial", "partners", "taxes", "revenue", "other"}
        allowed_dre = {"gross_revenue", "deductions", "direct_costs", "operating_expenses", "other_income", "other_expenses", "ignore", "auto"}
        if "comportamento" in opts and opts["comportamento"] not in allowed_behavior:
            raise ValueError("comportamento inválido")
        if "grupo" in opts and opts["grupo"] not in allowed_group:
            raise ValueError("grupo inválido")
        if "dre" in opts and opts["dre"] not in allowed_dre:
            raise ValueError("linha DRE inválida")
        kwargs = {}
        if "comportamento" in opts:
            kwargs["cost_behavior"] = opts["comportamento"]
        if "grupo" in opts:
            kwargs["managerial_group"] = opts["grupo"]
        if "dre" in opts:
            kwargs["dre_line"] = opts["dre"]
        if "notas" in opts:
            kwargs["notes"] = opts["notas"]
        if "opex" in opts:
            kwargs["is_opex"] = opts["opex"].casefold() in {"1", "sim", "s", "true", "yes"}
        db = context.application.bot_data["bling"].cash_db
        updated = await asyncio.to_thread(db.set_category_classification, category, **kwargs)
        rows = await asyncio.to_thread(db.classification_rows)
        cls = next((r for r in rows if str(r["category_id"]) == str(updated["category_id"])), None) or {}
        await update.effective_message.reply_text(
            f"✅ Categoria atualizada: {updated['description']}\n"
            f"Comportamento: {cls.get('cost_behavior', 'other')}\n"
            f"Grupo: {cls.get('managerial_group', 'other')}\n"
            f"DRE: {cls.get('dre_line', 'auto')}\n"
            f"OPEX: {'sim' if cls.get('is_opex') else 'não'}"
        )
    except (ValueError, IndexError) as exc:
        await update.effective_message.reply_text(f"⚠️ {exc}")


async def configurar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    db = context.application.bot_data["bling"].cash_db
    raw = " ".join(context.args).strip()
    if not raw:
        settings = await asyncio.to_thread(db.all_settings)
        await update.effective_message.reply_text(
            "⚙️ Configurações gerenciais\n\n"
            f"Dias úteis/mês: {settings.get('workdays_per_month', '21')}\n"
            f"Horas/dia: {settings.get('workhours_per_day', '8')}\n"
            f"Anomalia: +{settings.get('anomaly_percent_threshold', '30')}%\n"
            f"Valor mínimo anomalia: R$ {settings.get('anomaly_min_amount', '100')}\n"
            f"Pequena despesa padrão: R$ {settings.get('small_expense_threshold', '100')}\n\n"
            "Alterar, por exemplo:\n"
            "/configurar dias_uteis=21 horas_dia=8 anomalia_pct=30 anomalia_min=100 pequenas=100"
        )
        return
    mapping = {
        "dias_uteis": "workdays_per_month",
        "horas_dia": "workhours_per_day",
        "anomalia_pct": "anomaly_percent_threshold",
        "anomalia_min": "anomaly_min_amount",
        "pequenas": "small_expense_threshold",
    }
    changed = []
    try:
        for token in shlex.split(raw):
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            if key not in mapping:
                raise ValueError(f"Configuração desconhecida: {key}")
            Decimal(value.replace(",", "."))
            await asyncio.to_thread(db.set_setting, mapping[key], value.replace(",", "."))
            changed.append(key)
        if not changed:
            raise ValueError("Nenhuma configuração reconhecida.")
        await update.effective_message.reply_text("✅ Configurações atualizadas: " + ", ".join(changed))
    except (ValueError, ArithmeticError) as exc:
        await update.effective_message.reply_text(f"⚠️ {exc}")


async def _run_management_report(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    tz: ZoneInfo = context.application.bot_data["timezone"]
    service: ReportService = context.application.bot_data["reports"]
    bling: BlingClient = context.application.bot_data["bling"]
    today = datetime.now(tz).date()
    # Keep the recent window fresh before querying SQLite. Historical periods are
    # never rescanned here; missing coverage is reported with /sincronizar guidance.
    try:
        await bling.sync_cash_recent(today, force=False)
    except (BlingAuthError, BlingAPIError) as exc:
        logger.warning("Relatório usando cache local porque a sincronização recente falhou: %s", exc)
    try:
        output = await asyncio.to_thread(service.answer, text, today)
        target = update.effective_message
        if target:
            await send_long_message(target, output, reply_markup=reports_keyboard())
        elif update.callback_query:
            await update.callback_query.edit_message_text("✅ Relatório gerado abaixo.")
            await send_long_message(update.callback_query.message, output, reply_markup=reports_keyboard())
    except Exception as exc:
        logger.exception("Erro ao gerar relatório gerencial")
        msg = update.effective_message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text(f"⚠️ Não foi possível gerar o relatório: {str(exc)[:700]}")


async def dre_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context): return
    await _run_management_report(update, context, "DRE " + " ".join(context.args))


async def fornecedores_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context): return
    await _run_management_report(update, context, "maiores fornecedores " + " ".join(context.args))


async def recorrentes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context): return
    await _run_management_report(update, context, "despesas recorrentes " + " ".join(context.args))


async def opex_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context): return
    await _run_management_report(update, context, "OPEX custo por dia e hora " + " ".join(context.args))


async def anomalias_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context): return
    await _run_management_report(update, context, "gastos fora do padrão " + " ".join(context.args))


async def natural_language_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed(update, context):
        return
    message = update.effective_message
    if not message or not message.text:
        return
    # OAuth has priority.
    if _pending_oauth_is_valid(context):
        await oauth_callback_message(update, context)
        return
    if context.user_data.get(CALIBRATION_PENDING_KEY):
        bling: BlingClient = context.application.bot_data["bling"]
        tz: ZoneInfo = context.application.bot_data["timezone"]
        try:
            balances = {}
            for piece in message.text.split(";"):
                if "=" not in piece:
                    continue
                name, value = piece.split("=",1)
                balances[name.strip()] = _parse_decimal_br(value)
            if not balances:
                raise ValueError("Nenhum saldo reconhecido. Separe as contas por ponto e vírgula.")
            accounts = await bling.calibrate_cash_balances(balances, datetime.now(tz).date())
            context.user_data.pop(CALIBRATION_PENDING_KEY, None)
            await message.reply_text("✅ Saldos calibrados. A partir de agora o banco local atualiza os saldos com os novos lançamentos do Bling.\n\n" + "\n".join(f"• {a.description}" for a in accounts))
        except Exception as exc:
            await message.reply_text(f"⚠️ Não consegui calibrar: {exc}\nTente novamente ou envie /menu para cancelar.")
        return
    if context.user_data.get(ENTRY_DRAFT_KEY):
        if await handle_entry_text(update, context):
            return
    if context.user_data.get(CARD_DRAFT_KEY):
        if await handle_financial_card_text(update, context):
            return
    card_query = detect_financial_card_query(message.text)
    if card_query:
        kind, contact_query, period = card_query
        await start_financial_card_from_natural_language(
            update, context, kind, contact_query, period
        )
        return
    await _run_management_report(update, context, message.text)


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
    lines = [
        "💳 Saldos — Caixas e Bancos",
        f"📅 Posição até {summary.as_of.strftime('%d/%m/%Y')}",
        "",
    ]
    if not summary.accounts:
        lines.append("Nenhum lançamento financeiro foi retornado pelo Bling.")
    else:
        for account in summary.accounts:
            value = format_brl(account.balance) if account.balance is not None else "⚠️ calibrar"
            lines.append(f"🏦 {account.description}: {value}")
        lines.append("")
        if summary.fully_calibrated:
            lines.append(f"💵 Saldo total calculado: {format_brl(summary.total_balance)}")
        else:
            lines.append("⚠️ Há contas sem saldo-base calibrado. Use /calibrar uma vez para ancorar o saldo atual do Bling.")
        lines.extend([
            "",
            "ℹ️ Depois da calibração, o SQLite mantém o saldo com sincronização incremental dos lançamentos do Bling.",
        ])
    return "\n".join(lines)


async def _send_cash_summary(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool,
    force_refresh: bool,
) -> None:
    bling: BlingClient = context.application.bot_data["bling"]
    tz: ZoneInfo = context.application.bot_data["timezone"]
    today = datetime.now(tz).date()
    auth_error = False
    accounts: tuple = ()
    try:
        summary = await bling.get_cash_summary(today, force_refresh=force_refresh)
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
            account_id, today, force_refresh=force_refresh
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
        month_net = month_credits - month_debits
        opening_month_balance = (account.balance - month_net) if account.balance is not None else None
        lines = [
            f"🏦 {account.description}",
            "",
            f"💳 Saldo calculado: {format_brl(account.balance) if account.balance is not None else '⚠️ não calibrado'}",
            "",
            f"Saldo no início do mês: {format_brl(opening_month_balance) if opening_month_balance is not None else '—'}",
            f"Entradas no mês: {format_brl(month_credits)}",
            f"Saídas no mês: {format_brl(month_debits)}",
            f"Movimento líquido no mês: {format_brl(month_net)}",
            f"Lançamentos no histórico: {account.movement_count}",
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
    tz: ZoneInfo = context.application.bot_data["timezone"]
    today = datetime.now(tz).date()
    auth_error = False
    try:
        cash = await bling.get_cash_summary(today)
        flow = await bling.get_financial_summary(start, end)
        if not cash.fully_calibrated:
            text = (
                "⚠️ Para calcular a posição financeira com saldo atual, primeiro calibre as contas com /calibrar.\n\n"
                "A API pública do Bling fornece os lançamentos, mas não o saldo inicial/atual das contas no catálogo."
            )
            keyboard = sync_keyboard()
            if edit and update.callback_query:
                await update.callback_query.edit_message_text(text, reply_markup=keyboard)
            elif update.effective_message:
                await update.effective_message.reply_text(text, reply_markup=keyboard)
            return
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
            "ℹ️ Baseado nas contas financeiras atuais e nos lançamentos válidos do Bling; "
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

    if data.startswith("entry:"):
        if await handle_entry_callback(update, context, data):
            return

    if data.startswith("card:"):
        if await handle_financial_card_callback(update, context, data):
            return

    if data == "menu":
        clear_entry_draft(context)
        clear_financial_card_draft(context)
        context.user_data.pop(CALIBRATION_PENDING_KEY, None)
        await query.edit_message_text(
            "Financeiro Bling — escolha uma opção:", reply_markup=menu_keyboard()
        )
        return

    if data == "cash:menu":
        await query.edit_message_text(
            "⏳ Sincronizando as contas atuais e os saldos do Bling...\n"
            "Na primeira consulta precisa validar os lançamentos que realmente afetam saldo."
        )
        await _send_cash_summary(update, context, edit=True, force_refresh=False)
        return

    if data == "cash:refresh":
        await query.edit_message_text(
            "⏳ Atualizando saldos de Caixas e Bancos...\n"
            "Validando novamente as contas atuais e os lançamentos do período."
        )
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

    if data == "reports:menu":
        await query.edit_message_text(
            "📚 Relatórios gerenciais\n\nToque numa opção ou escreva sua pergunta em linguagem natural.",
            reply_markup=reports_keyboard(),
        )
        return

    if data.startswith("reports:"):
        code = data.split(":", 1)[1]
        prompts = {
            "catdist": "Para onde foram cada R$ 100 que gastei este mês?",
            "catrank": "Quais as 10 categorias que mais consumiram dinheiro este mês?",
            "suprank": "Quais foram meus 10 maiores fornecedores este mês?",
            "suphistory": "Quais foram meus maiores fornecedores este mês?",
            "recurring": "Quais são minhas despesas recorrentes nos últimos 12 meses?",
            "fixedvar": "Quanto tenho de despesas fixas x variáveis este mês?",
            "evolution": "Mostre a evolução das despesas nos últimos 12 meses",
            "variation": "O que mais aumentou este mês em relação ao mês passado?",
            "partners": "Quanto foi pago de pró-labore e retiradas este ano?",
            "admin": "Qual meu custo administrativo este ano?",
            "operational": "Quanto gastei com operação este ano?",
            "dre": "Me mostre a DRE deste mês",
            "opex": "Quanto custa um dia e uma hora da empresa este mês?",
            "small": "Quanto gastei em despesas abaixo de R$ 100 este ano?",
            "anomaly": "Tem algum gasto fora do padrão este mês?",
        }
        if code == "suphistory":
            await query.edit_message_text(
                "👤 Histórico de fornecedor\n\n"
                "Escreva o nome, CPF/CNPJ e o período. Exemplos:\n"
                "• Quanto já paguei para Google em 2026?\n"
                "• Mostre o histórico do fornecedor OpenAI desde 2024.\n"
                "• Quanto paguei para 00.000.000/0001-00 este ano?",
                reply_markup=reports_keyboard(),
            )
            return
        prompt = prompts.get(code)
        if not prompt:
            await query.edit_message_text("Opção inválida.", reply_markup=reports_keyboard())
            return
        await _run_management_report(update, context, prompt)
        return

    if data == "sync:menu":
        await query.edit_message_text(
            "🔄 Sincronização do banco local\n\nNo uso normal só a janela recente é consultada. Histórico antigo só é reconsultado com /sincronizar INICIO FIM.",
            reply_markup=sync_keyboard(),
        )
        return

    if data == "sync:recent":
        bling: BlingClient = context.application.bot_data["bling"]
        tz: ZoneInfo = context.application.bot_data["timezone"]
        today = datetime.now(tz).date()
        await query.edit_message_text("⏳ Atualizando a janela recente no SQLite...")
        try:
            start, end, count = await bling.sync_cash_recent(today, force=True)
            await query.edit_message_text(
                f"✅ Atualizado: {format_period(start,end)}\nRegistros recebidos: {count}",
                reply_markup=sync_keyboard(),
            )
        except Exception as exc:
            await query.edit_message_text(f"⚠️ Falha: {str(exc)[:600]}", reply_markup=sync_keyboard())
        return

    if data == "sync:status":
        bling: BlingClient = context.application.bot_data["bling"]
        st = await bling.get_cash_sync_status()
        await query.edit_message_text(
            "🗄 Banco financeiro local\n\n"
            f"Lançamentos: {st.movements}\nContas ativas: {st.enabled_accounts}\n"
            f"Saldos calibrados: {st.calibrated_accounts}/{st.enabled_accounts}\nCategorias: {st.categories}\n"
            f"Cobertura: {st.oldest_date or '—'} até {st.newest_date or '—'}",
            reply_markup=sync_keyboard(),
        )
        return

    if data == "sync:calibrate":
        context.user_data[CALIBRATION_PENDING_KEY] = True
        bling: BlingClient = context.application.bot_data["bling"]
        accounts = await bling.list_local_cash_accounts(enabled_only=True)
        sample = "; ".join(f"{a.description}=0,00" for a in accounts) if accounts else "Conta=0,00"
        await query.edit_message_text(
            "💳 Calibração\n\nEnvie agora uma mensagem com os saldos atuais exibidos no Bling, por exemplo:\n" + sample
        )
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
            BotCommand("lancar", "Adicionar conta ou lançamento de caixa"),
            BotCommand("nova_pagar", "Criar uma conta a pagar"),
            BotCommand("nova_receber", "Criar uma conta a receber"),
            BotCommand("caixa_saida", "Registrar pagamento à vista"),
            BotCommand("caixa_entrada", "Registrar recebimento à vista"),
            BotCommand("saldos", "Saldos de Caixas e Bancos"),
            BotCommand("posicao", "Saldo atual + projeção financeira"),
            BotCommand("fluxo", "Fluxo por período ou datas livres"),
            BotCommand("pagar", "Contas a pagar"),
            BotCommand("receber", "Contas a receber"),
            BotCommand("ficha", "Ficha financeira por fornecedor/cliente"),
            BotCommand("ficha_pagar", "Quanto devo a um fornecedor"),
            BotCommand("ficha_receber", "Quanto um cliente me deve"),
            BotCommand("autorizar", "Autorizar ou reautorizar o Bling"),
            BotCommand("status_bling", "Ver status da conexão com o Bling"),
            BotCommand("relatorios", "Abrir relatórios gerenciais"),
            BotCommand("sincronizar", "Atualizar banco local ou um período"),
            BotCommand("status_sync", "Status da sincronização SQLite"),
            BotCommand("calibrar", "Calibrar saldos atuais uma vez"),
            BotCommand("contas", "Ver/gerenciar contas financeiras"),
            BotCommand("categorias", "Ver classificações gerenciais"),
            BotCommand("dre", "DRE gerencial"),
            BotCommand("fornecedores", "Ranking de fornecedores"),
            BotCommand("recorrentes", "Despesas recorrentes"),
            BotCommand("opex", "Custo por dia e hora"),
            BotCommand("anomalias", "Gastos fora do padrão"),
            BotCommand("configurar", "Configurações gerenciais"),
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
        cash_history_start=settings.cash_history_start,
        cash_db_file=settings.cash_db_file,
        cash_bootstrap_days=settings.cash_bootstrap_days,
        cash_sync_days=settings.cash_sync_days,
        cash_account_discovery_days=settings.cash_account_discovery_days,
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
    application.bot_data["reports"] = ReportService(
        bling.cash_db,
        ai_api_key=settings.openai_api_key,
        ai_model=settings.openai_model,
        ai_base_url=settings.openai_base_url,
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("lancar", lancar_command))
    application.add_handler(CommandHandler("nova_pagar", nova_pagar_command))
    application.add_handler(CommandHandler("nova_receber", nova_receber_command))
    application.add_handler(CommandHandler("caixa_saida", caixa_saida_command))
    application.add_handler(CommandHandler("caixa_entrada", caixa_entrada_command))
    application.add_handler(CommandHandler("pagar", pay_command))
    application.add_handler(CommandHandler("receber", receive_command))
    application.add_handler(CommandHandler("ficha", ficha_command))
    application.add_handler(CommandHandler("ficha_pagar", ficha_pagar_command))
    application.add_handler(CommandHandler("ficha_receber", ficha_receber_command))
    application.add_handler(CommandHandler("saldos", saldos_command))
    application.add_handler(CommandHandler("posicao", posicao_command))
    application.add_handler(CommandHandler("fluxo", fluxo_command))
    application.add_handler(CommandHandler("autorizar", authorize_command))
    application.add_handler(CommandHandler("status_bling", bling_status_command))
    application.add_handler(CommandHandler("relatorios", relatorios_command))
    application.add_handler(CommandHandler("sincronizar", sincronizar_command))
    application.add_handler(CommandHandler("status_sync", status_sync_command))
    application.add_handler(CommandHandler("contas", contas_command))
    application.add_handler(CommandHandler("ativar_conta", ativar_conta_command))
    application.add_handler(CommandHandler("desativar_conta", desativar_conta_command))
    application.add_handler(CommandHandler("calibrar", calibrar_command))
    application.add_handler(CommandHandler("categorias", categorias_command))
    application.add_handler(CommandHandler("classificar", classificar_command))
    application.add_handler(CommandHandler("dre", dre_command))
    application.add_handler(CommandHandler("fornecedores", fornecedores_command))
    application.add_handler(CommandHandler("recorrentes", recorrentes_command))
    application.add_handler(CommandHandler("opex", opex_command))
    application.add_handler(CommandHandler("anomalias", anomalias_command))
    application.add_handler(CommandHandler("configurar", configurar_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, natural_language_message)
    )
    application.add_error_handler(error_handler)

    logger.info("Iniciando bot Telegram em polling.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
