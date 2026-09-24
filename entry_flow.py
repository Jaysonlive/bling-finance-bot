from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from bling import BlingAPIError, BlingAuthError, BlingClient

ENTRY_DRAFT_KEY = "financial_entry_draft"

KIND_LABELS = {
    "payable": "Conta a pagar",
    "receivable": "Conta a receber",
    "cash_out": "Saída à vista",
    "cash_in": "Entrada à vista",
}


def entry_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💸 Nova conta a pagar", callback_data="entry:start:payable")],
            [InlineKeyboardButton("💰 Nova conta a receber", callback_data="entry:start:receivable")],
            [
                InlineKeyboardButton("🧾 Saída à vista", callback_data="entry:start:cash_out"),
                InlineKeyboardButton("💵 Entrada à vista", callback_data="entry:start:cash_in"),
            ],
            [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
        ]
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancelar lançamento", callback_data="entry:cancel")]]
    )


def _format_brl(value: Decimal) -> str:
    value = value.quantize(Decimal("0.01"))
    raw = f"{value:,.2f}"
    return "R$ " + raw.replace(",", "X").replace(".", ",").replace("X", ".")


def _parse_amount(text: str) -> Decimal:
    raw = text.strip().replace("R$", "").replace(" ", "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Valor inválido. Exemplo: 1250,90") from exc
    if value <= 0:
        raise ValueError("O valor precisa ser maior que zero.")
    return value.quantize(Decimal("0.01"))


def _today(context: ContextTypes.DEFAULT_TYPE) -> date:
    tz = context.application.bot_data["timezone"]
    return datetime.now(tz).date()


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
    raise ValueError("Data inválida. Use DD/MM/AAAA, AAAA-MM-DD, hoje, ontem ou amanhã.")


def _date_label(value: str | date | None) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%d/%m/%Y")


def _draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    value = context.user_data.get(ENTRY_DRAFT_KEY)
    return value if isinstance(value, dict) else None


def _set_stage(context: ContextTypes.DEFAULT_TYPE, stage: str) -> dict[str, Any]:
    draft = _draft(context)
    if draft is None:
        raise RuntimeError("Nenhum lançamento em andamento.")
    draft["stage"] = stage
    return draft


def clear_entry_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(ENTRY_DRAFT_KEY, None)


async def start_entry_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    kind: str,
    *,
    edit: bool = False,
) -> None:
    if kind not in KIND_LABELS:
        raise ValueError("Tipo de lançamento inválido.")
    context.user_data[ENTRY_DRAFT_KEY] = {
        "kind": kind,
        "stage": "contact_query",
        "options": [],
    }
    role = "fornecedor" if kind in {"payable", "cash_out"} else "cliente"
    text = (
        f"➕ {KIND_LABELS[kind]}\n\n"
        f"1/7 — Digite o nome, CPF ou CNPJ do {role}.\n"
        "Vou pesquisar diretamente nos contatos do Bling."
    )
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_cancel_keyboard())
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=_cancel_keyboard())


def _options_keyboard(prefix: str, options: list[dict[str, Any]], *, allow_skip: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for idx, item in enumerate(options[:10]):
        label = str(item.get("label") or item.get("name") or item.get("description") or f"Opção {idx+1}")
        if len(label) > 48:
            label = label[:45] + "..."
        rows.append([InlineKeyboardButton(label, callback_data=f"entry:{prefix}:{idx}")])
    if allow_skip:
        rows.append([InlineKeyboardButton("⏭ Sem conta financeira por enquanto", callback_data="entry:skip_account")])
    rows.append([InlineKeyboardButton("❌ Cancelar", callback_data="entry:cancel")])
    return InlineKeyboardMarkup(rows)


async def _prompt_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _set_stage(context, "category_query")
    await update.effective_message.reply_text(
        "5/7 — Digite o nome da categoria da receita/despesa.\n\n"
        "Exemplos: Software, Combustíveis, Segurança Eletrônica, Custo dos produtos vendidos.",
        reply_markup=_cancel_keyboard(),
    )


async def _prompt_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = _draft(context)
    assert draft is not None
    bling: BlingClient = context.application.bot_data["bling"]
    accounts = await bling.list_local_cash_accounts(enabled_only=True)
    options = [
        {"id": a.account_id, "description": a.description, "label": a.description}
        for a in accounts
    ]
    if not options:
        # Fallback only for a fresh installation before account activation.
        catalog = await bling.list_financial_accounts_catalog()
        options = [
            {"id": row["account_id"], "description": row["description"], "label": row["description"]}
            for row in catalog[:10]
        ]
    draft["options"] = options
    draft["stage"] = "account_pick"
    cash_required = draft["kind"] in {"cash_out", "cash_in"}
    text = (
        "6/7 — Escolha a conta financeira."
        if cash_required
        else "6/7 — Escolha a conta financeira prevista, ou deixe sem conta por enquanto."
    )
    await update.effective_message.reply_text(
        text,
        reply_markup=_options_keyboard("account", options, allow_skip=not cash_required),
    )


async def _prompt_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _set_stage(context, "history")
    await update.effective_message.reply_text(
        "7/7 — Digite um histórico/descrição para o lançamento ou toque em Pular.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⏭ Pular histórico", callback_data="entry:skip_history")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="entry:cancel")],
            ]
        ),
    )


def _summary_text(draft: dict[str, Any]) -> str:
    kind = str(draft["kind"])
    lines = [f"✅ Conferir — {KIND_LABELS[kind]}", ""]
    role = "Fornecedor" if kind in {"payable", "cash_out"} else "Cliente"
    lines.append(f"{role}: {draft.get('contact_name', '—')}")
    lines.append(f"Valor: {_format_brl(Decimal(str(draft.get('amount', '0'))))}")
    lines.append(f"Competência: {_date_label(draft.get('competence'))}")
    if kind in {"payable", "receivable"}:
        lines.append(f"Vencimento: {_date_label(draft.get('due_date'))}")
    else:
        lines.append(f"Data do caixa: {_date_label(draft.get('movement_date'))}")
    lines.append(f"Categoria: {draft.get('category_name', '—')}")
    lines.append(f"Conta financeira: {draft.get('account_name') or 'Não definida'}")
    if draft.get("history"):
        lines.append(f"Histórico: {draft['history']}")
    lines.append("")
    lines.append("Nada será gravado antes de você confirmar.")
    return "\n".join(lines)


async def _show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = _set_stage(context, "confirm")
    await update.effective_message.reply_text(
        _summary_text(draft),
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Confirmar e lançar no Bling", callback_data="entry:confirm")],
                [InlineKeyboardButton("🔄 Recomeçar", callback_data=f"entry:restart:{draft['kind']}")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="entry:cancel")],
            ]
        ),
    )


async def handle_entry_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle a text message if an entry wizard is active. Returns True if consumed."""
    draft = _draft(context)
    message = update.effective_message
    if draft is None or message is None or not message.text:
        return False
    text = message.text.strip()
    stage = str(draft.get("stage") or "")
    bling: BlingClient = context.application.bot_data["bling"]

    try:
        if stage == "contact_query":
            await update.effective_chat.send_action(ChatAction.TYPING)
            options = await bling.search_contacts(text, limit=8)
            if not options:
                await message.reply_text(
                    "Não encontrei esse contato no Bling. Digite outro nome/CPF/CNPJ. "
                    "O contato precisa existir no Bling antes do lançamento.",
                    reply_markup=_cancel_keyboard(),
                )
                return True
            prepared = []
            for item in options:
                doc = item.get("document") or ""
                label = item["name"] + (f" — {doc}" if doc else "")
                prepared.append({**item, "label": label})
            draft["options"] = prepared
            draft["stage"] = "contact_pick"
            await message.reply_text(
                "Selecione o contato correto:",
                reply_markup=_options_keyboard("contact", prepared),
            )
            return True

        if stage == "amount":
            amount = _parse_amount(text)
            draft["amount"] = str(amount)
            draft["stage"] = "competence"
            await message.reply_text(
                "3/7 — Informe a data de competência.\nEx.: hoje, 24/09/2026 ou 2026-09-24.",
                reply_markup=_cancel_keyboard(),
            )
            return True

        if stage == "competence":
            competence = _parse_date(text, context)
            draft["competence"] = competence.isoformat()
            if draft["kind"] in {"payable", "receivable"}:
                draft["stage"] = "due_date"
                await message.reply_text(
                    "4/7 — Informe a data de vencimento.", reply_markup=_cancel_keyboard()
                )
            else:
                draft["stage"] = "movement_date"
                await message.reply_text(
                    "4/7 — Informe a data em que foi pago/recebido à vista.\nPode responder hoje.",
                    reply_markup=_cancel_keyboard(),
                )
            return True

        if stage == "due_date":
            due = _parse_date(text, context)
            draft["due_date"] = due.isoformat()
            await _prompt_category(update, context)
            return True

        if stage == "movement_date":
            movement = _parse_date(text, context)
            draft["movement_date"] = movement.isoformat()
            await _prompt_category(update, context)
            return True

        if stage == "category_query":
            try:
                await bling.sync_categories(force=False)
            except (BlingAPIError, BlingAuthError):
                pass
            wanted_type = 1 if draft["kind"] in {"payable", "cash_out"} else 2
            options = await asyncio.to_thread(
                bling.cash_db.search_categories, text, limit=8, category_type=wanted_type
            )
            if not options:
                try:
                    await bling.sync_categories(force=True)
                except (BlingAPIError, BlingAuthError):
                    pass
                options = await asyncio.to_thread(
                    bling.cash_db.search_categories, text, limit=8, category_type=wanted_type
                )
            if not options:
                await message.reply_text(
                    "Não encontrei essa categoria. Digite outro nome. A categoria precisa existir no Bling.",
                    reply_markup=_cancel_keyboard(),
                )
                return True
            prepared = [
                {
                    "id": str(item["category_id"]),
                    "description": str(item["description"]),
                    "label": str(item["description"]),
                }
                for item in options
            ]
            draft["options"] = prepared
            draft["stage"] = "category_pick"
            await message.reply_text(
                "Selecione a categoria correta:",
                reply_markup=_options_keyboard("category", prepared),
            )
            return True

        if stage == "account_pick":
            # Text fallback for users who prefer typing the account name.
            needle = re.sub(r"\s+", " ", text.casefold()).strip()
            matches = [
                item for item in draft.get("options", [])
                if needle in str(item.get("description") or "").casefold()
            ]
            if len(matches) == 1:
                item = matches[0]
                draft["account_id"] = str(item["id"])
                draft["account_name"] = str(item["description"])
                await _prompt_history(update, context)
            else:
                await message.reply_text("Escolha uma das contas pelos botões acima.")
            return True

        if stage == "history":
            draft["history"] = text[:2000]
            await _show_confirmation(update, context)
            return True

        if stage == "confirm":
            await message.reply_text("Use os botões Confirmar, Recomeçar ou Cancelar da mensagem anterior.")
            return True

    except ValueError as exc:
        await message.reply_text(f"⚠️ {exc}", reply_markup=_cancel_keyboard())
        return True
    except BlingAuthError:
        await message.reply_text("🔐 O Bling precisa ser autorizado. Use /autorizar e depois retome o lançamento.")
        return True
    except BlingAPIError as exc:
        await message.reply_text(f"⚠️ O Bling recusou a consulta: {str(exc)[:700]}")
        return True

    return False


async def handle_entry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> bool:
    """Handle callback_data starting with entry:. Returns True when consumed."""
    if not data.startswith("entry:"):
        return False
    query = update.callback_query
    if query is None:
        return True

    if data == "entry:menu":
        clear_entry_draft(context)
        await query.edit_message_text(
            "➕ Lançamentos financeiros\n\nEscolha o que deseja lançar no Bling:",
            reply_markup=entry_menu_keyboard(),
        )
        return True

    if data.startswith("entry:start:"):
        kind = data.split(":", 2)[2]
        await start_entry_flow(update, context, kind, edit=True)
        return True

    if data.startswith("entry:restart:"):
        kind = data.split(":", 2)[2]
        await start_entry_flow(update, context, kind, edit=True)
        return True

    if data == "entry:cancel":
        clear_entry_draft(context)
        await query.edit_message_text(
            "Lançamento cancelado.",
            reply_markup=entry_menu_keyboard(),
        )
        return True

    draft = _draft(context)
    if draft is None:
        await query.edit_message_text(
            "Esse lançamento não está mais ativo. Inicie novamente.",
            reply_markup=entry_menu_keyboard(),
        )
        return True

    if data.startswith("entry:contact:"):
        try:
            idx = int(data.rsplit(":", 1)[1])
            item = draft.get("options", [])[idx]
        except (ValueError, IndexError, TypeError):
            await query.answer("Contato inválido.", show_alert=True)
            return True
        draft["contact_id"] = str(item["id"])
        draft["contact_name"] = str(item["name"])
        draft["contact_document"] = str(item.get("document") or "")
        draft["stage"] = "amount"
        await query.edit_message_text(
            f"Contato: {draft['contact_name']}\n\n2/7 — Informe o valor.\nEx.: 1250,90",
            reply_markup=_cancel_keyboard(),
        )
        return True

    if data.startswith("entry:category:"):
        try:
            idx = int(data.rsplit(":", 1)[1])
            item = draft.get("options", [])[idx]
        except (ValueError, IndexError, TypeError):
            await query.answer("Categoria inválida.", show_alert=True)
            return True
        draft["category_id"] = str(item["id"])
        draft["category_name"] = str(item["description"])
        # edit current picker, then issue account picker as a new message so callback state stays simple
        await query.edit_message_text(f"Categoria: {draft['category_name']}")
        fake_update = update
        await _prompt_accounts(fake_update, context)
        return True

    if data.startswith("entry:account:"):
        try:
            idx = int(data.rsplit(":", 1)[1])
            item = draft.get("options", [])[idx]
        except (ValueError, IndexError, TypeError):
            await query.answer("Conta inválida.", show_alert=True)
            return True
        draft["account_id"] = str(item["id"])
        draft["account_name"] = str(item["description"])
        await query.edit_message_text(f"Conta financeira: {draft['account_name']}")
        await _prompt_history(update, context)
        return True

    if data == "entry:skip_account":
        if draft["kind"] in {"cash_out", "cash_in"}:
            await query.answer("Para caixa à vista a conta financeira é obrigatória.", show_alert=True)
            return True
        draft["account_id"] = None
        draft["account_name"] = None
        await query.edit_message_text("Conta financeira: não definida")
        await _prompt_history(update, context)
        return True

    if data == "entry:skip_history":
        if draft["kind"] in {"cash_out", "cash_in"}:
            draft["history"] = "Lançamento pelo Telegram"
            await query.edit_message_text("Histórico: Lançamento pelo Telegram")
        else:
            draft["history"] = ""
            await query.edit_message_text("Histórico: não informado")
        await _show_confirmation(update, context)
        return True

    if data == "entry:confirm":
        if draft.get("submitting"):
            await query.answer("O lançamento já está sendo enviado.", show_alert=True)
            return True
        draft["submitting"] = True
        await query.edit_message_text("⏳ Enviando lançamento para o Bling...")
        bling: BlingClient = context.application.bot_data["bling"]
        try:
            common = {
                "contact_id": str(draft["contact_id"]),
                "amount": Decimal(str(draft["amount"])),
                "competence": date.fromisoformat(str(draft["competence"])),
                "category_id": str(draft["category_id"]),
                "history": str(draft.get("history") or ""),
            }
            kind = str(draft["kind"])
            if kind == "payable":
                created_id = await bling.create_payable(
                    **common,
                    due_date=date.fromisoformat(str(draft["due_date"])),
                    financial_account_id=draft.get("account_id"),
                    emission_date=_today(context),
                )
            elif kind == "receivable":
                created_id = await bling.create_receivable(
                    **common,
                    due_date=date.fromisoformat(str(draft["due_date"])),
                    financial_account_id=draft.get("account_id"),
                    emission_date=_today(context),
                )
            elif kind in {"cash_out", "cash_in"}:
                created_id = await bling.create_cash_entry(
                    **common,
                    movement_date=date.fromisoformat(str(draft["movement_date"])),
                    financial_account_id=str(draft["account_id"]),
                    direction="D" if kind == "cash_out" else "C",
                )
            else:
                raise ValueError("Tipo de lançamento inválido.")

            result_summary = _summary_text(draft).replace("✅ Conferir", "✅ Lançado")
            clear_entry_draft(context)
            await query.edit_message_text(
                result_summary + f"\n\nID no Bling: {created_id}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("➕ Fazer outro lançamento", callback_data="entry:menu")],
                        [InlineKeyboardButton("⬅️ Menu", callback_data="menu")],
                    ]
                ),
            )
        except BlingAuthError as exc:
            draft["submitting"] = False
            await query.edit_message_text(
                "🔐 O Bling precisa ser reautorizado antes de gravar.\n\n" + str(exc)[:500],
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🔐 Autorizar Bling", callback_data="bling:authorize")],
                        [InlineKeyboardButton("❌ Cancelar", callback_data="entry:cancel")],
                    ]
                ),
            )
        except (BlingAPIError, ValueError) as exc:
            draft["submitting"] = False
            await query.edit_message_text(
                "⚠️ O lançamento NÃO foi confirmado no Bling.\n\n"
                f"Detalhe: {str(exc)[:900]}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🔁 Tentar novamente", callback_data="entry:confirm")],
                        [InlineKeyboardButton("🔄 Recomeçar", callback_data=f"entry:restart:{draft['kind']}")],
                        [InlineKeyboardButton("❌ Cancelar", callback_data="entry:cancel")],
                    ]
                ),
            )
        return True

    return True
