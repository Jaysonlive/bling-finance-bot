from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from bling import BlingAPIError, BlingAuthError, BlingClient, ContactAccountCard

CARD_DRAFT_KEY = "financial_card_draft"

KIND_LABELS = {
    "payable": "A pagar por fornecedor",
    "receivable": "A receber por cliente",
}

PERIOD_LABELS = {
    "today": "Hoje",
    "week": "Esta semana",
    "month": "Este mês",
    "year": "Este ano",
    "custom": "Entre datas",
}


def _normalize(value: str) -> str:
    raw = unicodedata.normalize("NFKD", value or "")
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.casefold()
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _format_brl(value: Decimal) -> str:
    value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    negative = value < 0
    raw = f"{abs(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if negative else ''}R$ {raw}"


def _format_period(start: date, end: date) -> str:
    if start == end:
        return start.strftime("%d/%m/%Y")
    return f"{start.strftime('%d/%m/%Y')} a {end.strftime('%d/%m/%Y')}"


def _today(context: ContextTypes.DEFAULT_TYPE) -> date:
    tz = context.application.bot_data["timezone"]
    return datetime.now(tz).date()


def _period(period: str, context: ContextTypes.DEFAULT_TYPE) -> tuple[date, date]:
    today = _today(context)
    if period == "today":
        return today, today
    if period == "week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if period == "month":
        return date(today.year, today.month, 1), date(
            today.year, today.month, calendar.monthrange(today.year, today.month)[1]
        )
    if period == "year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    raise ValueError("Período inválido.")


def _parse_date(text: str, context: ContextTypes.DEFAULT_TYPE) -> date:
    raw = text.strip().casefold()
    today = _today(context)
    aliases = {
        "hoje": today,
        "ontem": today - timedelta(days=1),
        "amanha": today + timedelta(days=1),
        "amanhã": today + timedelta(days=1),
    }
    if raw in aliases:
        return aliases[raw]
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError("Data inválida. Use DD/MM/AAAA ou AAAA-MM-DD.")


def financial_card_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💸 Quanto devo a um fornecedor", callback_data="card:start:payable")],
            [InlineKeyboardButton("💰 Quanto um cliente me deve", callback_data="card:start:receivable")],
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
        ]
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("❌ Cancelar", callback_data="card:cancel")],
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
        ]
    )


def _period_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Hoje", callback_data="card:period:today"),
                InlineKeyboardButton("Semana", callback_data="card:period:week"),
            ],
            [
                InlineKeyboardButton("Mês", callback_data="card:period:month"),
                InlineKeyboardButton("Ano", callback_data="card:period:year"),
            ],
            [InlineKeyboardButton("📅 Entre datas", callback_data="card:period:custom")],
            [InlineKeyboardButton("🔎 Trocar contato", callback_data="card:restart")],
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
        ]
    )


def _result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📅 Outro período", callback_data="card:periodmenu")],
            [InlineKeyboardButton("🔎 Outro contato", callback_data="card:restart")],
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
        ]
    )


def clear_financial_card_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(CARD_DRAFT_KEY, None)


def _draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    value = context.user_data.get(CARD_DRAFT_KEY)
    return value if isinstance(value, dict) else None


async def start_financial_card_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: str | None = None,
    *,
    edit: bool = False,
) -> None:
    if kind is None:
        clear_financial_card_draft(context)
        text = (
            "📒 Ficha financeira\n\n"
            "Consulte somente títulos que ainda estão em aberto ou parcialmente pagos/recebidos.\n"
            "Escolha o tipo de consulta:"
        )
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=financial_card_menu_keyboard())
        elif update.effective_message:
            await update.effective_message.reply_text(text, reply_markup=financial_card_menu_keyboard())
        return

    if kind not in KIND_LABELS:
        raise ValueError("Tipo de ficha financeira inválido.")
    context.user_data[CARD_DRAFT_KEY] = {
        "kind": kind,
        "stage": "contact_query",
        "options": [],
        "pending_period": None,
    }
    role = "fornecedor" if kind == "payable" else "cliente"
    text = (
        f"📒 {KIND_LABELS[kind]}\n\n"
        f"Digite o nome, CPF ou CNPJ do {role}.\n"
        "Vou localizar o contato no Bling e depois você escolhe o período de vencimento."
    )
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_cancel_keyboard())
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=_cancel_keyboard())


def _strip_period_words(subject: str) -> tuple[str, str | None]:
    n = _normalize(subject)
    candidates = [
        ("today", (" hoje",)),
        ("week", (" esta semana", " nessa semana", " na semana")),
        ("month", (" este mes", " nesse mes", " no mes")),
        ("year", (" este ano", " nesse ano", " no ano")),
    ]
    selected: str | None = None
    for code, markers in candidates:
        for marker in markers:
            if n.endswith(marker):
                n = n[: -len(marker)].strip()
                selected = code
                return n, selected
    return n, selected


def detect_financial_card_query(text: str) -> tuple[str, str, str | None] | None:
    """Conservative natural-language detector for open-title questions.

    It intentionally handles only clear debt/receivable phrases so ordinary
    management-report questions keep flowing to the existing report service.
    """
    n = _normalize(text).strip(" .?!")
    payable_patterns = [
        r"^quanto (?:eu )?devo (?:para|ao|a) (.+)$",
        r"^quanto (?:eu )?tenho a pagar (?:para|ao|a|de) (.+)$",
        r"^o que (?:eu )?devo (?:para|ao|a) (.+)$",
    ]
    receivable_patterns = [
        r"^quanto (?:eu )?tenho a receber (?:de|do|da) (.+)$",
        r"^quanto (?:o |a )?(.+?) me deve$",
        r"^o que (?:o |a )?(.+?) me deve$",
    ]
    for kind, patterns in (("payable", payable_patterns), ("receivable", receivable_patterns)):
        for pattern in patterns:
            match = re.match(pattern, n)
            if match:
                subject, period = _strip_period_words(match.group(1).strip())
                if len(subject) >= 2:
                    return kind, subject, period
    return None


async def start_financial_card_from_natural_language(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: str,
    contact_query: str,
    period: str | None,
) -> None:
    context.user_data[CARD_DRAFT_KEY] = {
        "kind": kind,
        "stage": "contact_query",
        "options": [],
        "pending_period": period,
    }
    await _search_and_offer_contacts(update, context, contact_query)


async def _search_and_offer_contacts(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    draft = _draft(context)
    if draft is None:
        return
    bling: BlingClient = context.application.bot_data["bling"]
    message = update.effective_message
    if message is None:
        return
    await update.effective_chat.send_action(ChatAction.TYPING)
    options = await bling.search_contacts(text, limit=8)
    if not options:
        await message.reply_text(
            "⚠️ Não encontrei esse contato no Bling. Tente outro nome, CPF ou CNPJ.",
            reply_markup=_cancel_keyboard(),
        )
        return
    draft["options"] = options
    rows: list[list[InlineKeyboardButton]] = []
    for idx, item in enumerate(options):
        label = str(item["name"])
        doc = str(item.get("document") or "")
        if doc:
            label += f" — {doc}"
        if len(label) > 58:
            label = label[:55] + "..."
        rows.append([InlineKeyboardButton(label, callback_data=f"card:contact:{idx}")])
    rows.append([InlineKeyboardButton("❌ Cancelar", callback_data="card:cancel")])
    role = "fornecedor" if draft["kind"] == "payable" else "cliente"
    await message.reply_text(
        f"Selecione o {role} correto:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def handle_financial_card_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    draft = _draft(context)
    if draft is None:
        return False
    message = update.effective_message
    if message is None or not message.text:
        return True
    stage = str(draft.get("stage") or "")
    try:
        if stage == "contact_query":
            await _search_and_offer_contacts(update, context, message.text)
            return True
        if stage == "custom_start":
            start = _parse_date(message.text, context)
            draft["start"] = start.isoformat()
            draft["stage"] = "custom_end"
            await message.reply_text(
                "📅 Agora informe a data final.\nEx.: 31/12/2026",
                reply_markup=_cancel_keyboard(),
            )
            return True
        if stage == "custom_end":
            end = _parse_date(message.text, context)
            start = date.fromisoformat(str(draft["start"]))
            if end < start:
                raise ValueError("A data final não pode ser anterior à data inicial.")
            draft["stage"] = "result"
            await update.effective_chat.send_action(ChatAction.TYPING)
            await _send_result(update, context, start, end, edit=False)
            return True
        if stage in {"period", "result"}:
            await message.reply_text("Use os botões da ficha financeira ou /ficha para começar outra consulta.")
            return True
    except ValueError as exc:
        await message.reply_text(f"⚠️ {exc}", reply_markup=_cancel_keyboard())
        return True
    except BlingAuthError:
        await message.reply_text("🔐 O Bling precisa ser autorizado. Use /autorizar.")
        return True
    except BlingAPIError as exc:
        await message.reply_text(f"⚠️ Não consegui consultar o Bling: {str(exc)[:700]}")
        return True
    return True


def _status_label(due: date, today: date, partial: bool) -> str:
    if due < today:
        days = (today - due).days
        status = f"🔴 vencido há {days} dia{'s' if days != 1 else ''}"
    elif due == today:
        status = "🟡 vence hoje"
    else:
        days = (due - today).days
        status = f"🟢 vence em {days} dia{'s' if days != 1 else ''}"
    if partial:
        status += " • parcial"
    return status


def _render_card(card: ContactAccountCard, contact_name: str, today: date) -> list[str]:
    payable = card.kind == "payable"
    title = "📒 FICHA FINANCEIRA — A PAGAR" if payable else "📒 FICHA FINANCEIRA — A RECEBER"
    role = "Fornecedor" if payable else "Cliente"
    total_label = "💸 Total devido" if payable else "💰 Total a receber"
    lines = [
        title,
        f"👤 {role}: {contact_name}",
        f"📅 Vencimentos: {_format_period(card.start, card.end)}",
        "",
        f"{total_label}: {_format_brl(card.total)}",
        f"📄 Títulos pendentes: {len(card.items)}",
    ]
    if not card.items:
        lines.extend(["", "✅ Nenhum título em aberto ou parcialmente liquidado nesse período."])
    else:
        lines.extend(["", "📆 VENCIMENTOS"])
        for idx, item in enumerate(card.items, 1):
            lines.append(
                f"{idx}. {item.due_date.strftime('%d/%m/%Y')} — {_format_brl(item.outstanding)}"
            )
            lines.append(f"   {_status_label(item.due_date, today, item.partial)}")
    # Telegram-safe chunks while preserving every title.
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = line if not current else current + "\n" + line
        if len(candidate) > 3700 and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def _send_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    start: date,
    end: date,
    *,
    edit: bool,
) -> None:
    draft = _draft(context)
    if draft is None:
        return
    bling: BlingClient = context.application.bot_data["bling"]
    kind = str(draft["kind"])
    contact_id = str(draft["contact_id"])
    contact_name = str(draft["contact_name"])
    try:
        card = await bling.get_contact_account_card(kind, contact_id, start, end)
        draft["stage"] = "result"
        draft["start"] = start.isoformat()
        draft["end"] = end.isoformat()
        chunks = _render_card(card, contact_name, _today(context))
        keyboard = _result_keyboard()
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(
                chunks[0], reply_markup=keyboard if len(chunks) == 1 else None
            )
            for idx, chunk in enumerate(chunks[1:], 1):
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=chunk,
                    reply_markup=keyboard if idx == len(chunks) - 1 else None,
                )
        elif update.effective_message:
            for idx, chunk in enumerate(chunks):
                await update.effective_message.reply_text(
                    chunk,
                    reply_markup=keyboard if idx == len(chunks) - 1 else None,
                )
    except BlingAuthError as exc:
        text = "🔐 O Bling precisa ser autorizado ou reautorizado antes da consulta.\n\n" + str(exc)[:500]
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text)
        elif update.effective_message:
            await update.effective_message.reply_text(text)
    except BlingAPIError as exc:
        text = "⚠️ Não foi possível consultar a ficha financeira no Bling.\n\n" + str(exc)[:800]
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=_result_keyboard())
        elif update.effective_message:
            await update.effective_message.reply_text(text, reply_markup=_result_keyboard())


async def handle_financial_card_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: str,
) -> bool:
    if not data.startswith("card:"):
        return False
    query = update.callback_query
    if query is None:
        return True

    if data == "card:menu":
        await start_financial_card_flow(update, context, None, edit=True)
        return True
    if data.startswith("card:start:"):
        await start_financial_card_flow(update, context, data.split(":", 2)[2], edit=True)
        return True
    if data == "card:cancel":
        clear_financial_card_draft(context)
        await query.edit_message_text(
            "Consulta cancelada.", reply_markup=financial_card_menu_keyboard()
        )
        return True
    if data == "card:restart":
        draft = _draft(context)
        kind = str(draft.get("kind")) if draft else None
        if kind in KIND_LABELS:
            await start_financial_card_flow(update, context, kind, edit=True)
        else:
            await start_financial_card_flow(update, context, None, edit=True)
        return True

    draft = _draft(context)
    if draft is None:
        await query.edit_message_text(
            "Essa ficha não está mais ativa. Inicie outra consulta.",
            reply_markup=financial_card_menu_keyboard(),
        )
        return True

    if data.startswith("card:contact:"):
        try:
            idx = int(data.rsplit(":", 1)[1])
            item = draft.get("options", [])[idx]
        except (ValueError, IndexError, TypeError):
            await query.answer("Contato inválido.", show_alert=True)
            return True
        draft["contact_id"] = str(item["id"])
        draft["contact_name"] = str(item["name"])
        draft["contact_document"] = str(item.get("document") or "")
        pending = draft.get("pending_period")
        if pending in {"today", "week", "month", "year"}:
            start, end = _period(str(pending), context)
            await query.edit_message_text("⏳ Consultando títulos pendentes no Bling...")
            await _send_result(update, context, start, end, edit=True)
            return True
        draft["stage"] = "period"
        await query.edit_message_text(
            f"👤 {draft['contact_name']}\n\nEscolha o período pelo vencimento dos títulos:",
            reply_markup=_period_keyboard(),
        )
        return True

    if data == "card:periodmenu":
        draft["stage"] = "period"
        await query.edit_message_text(
            f"👤 {draft.get('contact_name','Contato')}\n\nEscolha outro período pelo vencimento:",
            reply_markup=_period_keyboard(),
        )
        return True

    if data.startswith("card:period:"):
        period = data.split(":", 2)[2]
        if period == "custom":
            draft["stage"] = "custom_start"
            await query.edit_message_text(
                "📅 Informe a data inicial.\nEx.: 01/01/2026",
                reply_markup=_cancel_keyboard(),
            )
            return True
        if period not in {"today", "week", "month", "year"}:
            await query.answer("Período inválido.", show_alert=True)
            return True
        start, end = _period(period, context)
        await query.edit_message_text("⏳ Consultando títulos pendentes no Bling...")
        await _send_result(update, context, start, end, edit=True)
        return True

    return True
