from __future__ import annotations

import json
import logging
from datetime import date
from typing import Iterable

import httpx

logger = logging.getLogger(__name__)


class AIIntentInterpreter:
    """Optional natural-language normalizer using structured output.

    The model receives only the user's question and today's date. It never
    receives transaction rows, financial totals, balances or supplier lists and
    never performs the financial calculations. Its output is only a canonical
    query; ReportService and SQLite remain the source of truth.
    """

    def __init__(self, api_key: str | None, model: str, base_url: str = "https://api.openai.com/v1") -> None:
        self.api_key = (api_key or "").strip()
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.model)

    @staticmethod
    def _output_text(payload: dict) -> str:
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        for item in payload.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    value = content["text"].strip()
                    if value:
                        return value
        raise ValueError("A IA não retornou conteúdo estruturado.")

    def rewrite(
        self,
        user_text: str,
        today: date,
        *,
        categories: Iterable[str] = (),
        suppliers: Iterable[str] = (),
        accounts: Iterable[str] = (),
    ) -> str | None:
        # The iterable arguments remain for API compatibility with ReportService,
        # but are deliberately not sent to the model. Business data stays local.
        del categories, suppliers, accounts
        if not self.enabled:
            return None

        instruction = f"""
Você é um interpretador de intenção para um sistema financeiro empresarial brasileiro.
Hoje é {today.isoformat()}.
NÃO calcule valores, totais, médias, margens, percentuais, saldos ou conclusões financeiras.
NÃO invente categoria, fornecedor, conta, CPF/CNPJ ou valor.
Sua única tarefa é reescrever a pergunta em uma frase canônica curta em português, preservando os filtros escritos pelo usuário, para que um backend SQLite faça todos os cálculos.

Relatórios suportados: resumo financeiro; cada R$ 100 gastos; ranking de categorias; ranking de fornecedores; histórico de fornecedor; despesas recorrentes/assinaturas; fixas x variáveis; evolução mensal; variação por categoria; pró-labore/sócios; despesas administrativas; despesas operacionais; DRE; OPEX/custo por dia e hora; pequenas despesas; gastos fora do padrão/anomalias; busca de despesa por texto.

Resolva expressões relativas de tempo em datas explícitas quando isso melhorar a interpretação. Exemplos: hoje, ontem, esta semana, semana passada, este mês, mês passado, últimos 30 dias, últimos 3/6/12 meses, este ano, ano passado. Preserve comparações entre dois períodos.
""".strip()

        schema = {
            "type": "object",
            "properties": {
                "canonical_query": {"type": "string"},
            },
            "required": ["canonical_query"],
            "additionalProperties": False,
        }
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(
                    f"{self.base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "input": [
                            {"role": "developer", "content": instruction},
                            {"role": "user", "content": user_text},
                        ],
                        "text": {
                            "format": {
                                "type": "json_schema",
                                "name": "financial_intent",
                                "strict": True,
                                "schema": schema,
                            }
                        },
                        "max_output_tokens": 300,
                    },
                )
                response.raise_for_status()
                raw = self._output_text(response.json())
                data = json.loads(raw)
                value = data.get("canonical_query") if isinstance(data, dict) else None
                if isinstance(value, str) and 2 <= len(value.strip()) <= 1200:
                    return value.strip()
        except Exception as exc:
            # AI is an interpretation enhancement, not a dependency of financial
            # correctness. Local parsing continues if the provider is unavailable.
            logger.warning("Interpretação por IA indisponível; usando parser local: %s", exc)
        return None
