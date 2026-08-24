import base64
import json

import httpx

from app.models.schemas import (
    MentorVerdict,
    RulesEngineResult,
    SetupQualityResult,
    WeeklyReviewNarrativeResponse,
    WeeklyReviewResult,
    WhyExplanationResponse,
)
from config.settings import settings


class OllamaMentorError(RuntimeError):
    """Raised when the local Ollama instance is unreachable or returns invalid JSON."""


class OllamaClient:
    """
    Purpose:    Thin async wrapper around a local Ollama instance for Phase B
                ("Local LLM Mentor") deep-dive trade analysis and the
                conversational assistant (app/services/chat_context.py). Never
                calls any paid cloud LLM — this is the only AI egress point in
                the app, and it always targets settings.ollama_base_url.
    """

    def __init__(self, base_url: str | None = None) -> None:
        """
        Purpose:    Configure the client's target Ollama server.
        Args:       base_url (str | None): Override for settings.ollama_base_url,
                    mainly for tests.
        Returns:    None.
        Raises:     None.
        """
        self._base_url = (base_url or settings.ollama_base_url).rstrip("/")

    async def _generate_json(self, model: str, prompt: str, images: list[str] | None = None) -> dict:
        """
        Purpose:    Call Ollama's /api/generate with format="json" so the model
                    is constrained to emit a single valid JSON object, and parse it.
        Args:       model (str): Ollama model tag to run (must already be pulled locally).
                    prompt (str): Full prompt text, including the JSON schema instructions.
                    images (list[str] | None): Base64-encoded image payloads for
                        vision-capable models.
        Returns:    dict: Parsed JSON object returned by the model.
        Raises:     OllamaMentorError: If the request fails or the model's
                    response body is not valid JSON.
        """
        payload: dict = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"num_predict": 300},
        }
        if images:
            payload["images"] = images

        try:
            async with httpx.AsyncClient(timeout=settings.ollama_request_timeout_seconds) as client:
                response = await client.post(f"{self._base_url}/api/generate", json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            detail = str(exc) or type(exc).__name__
            raise OllamaMentorError(f"Local Ollama request failed: {detail}") from exc

        raw_text = response.json().get("response", "")
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise OllamaMentorError(f"Ollama returned non-JSON output: {raw_text!r}") from exc

    async def analyze_trade(
        self,
        rules_result: RulesEngineResult,
        trade_context: dict,
        screenshot_bytes: bytes | None = None,
    ) -> MentorVerdict:
        """
        Purpose:    Run the on-demand Phase B deep-dive: package the Phase A
                    rules-engine output plus raw trade context (and an optional
                    trade screenshot) and ask the local Ollama model for a
                    strictly-JSON good/bad verdict with reasoning. NEVER call
                    this automatically on trade close — it is strictly a
                    user-triggered "Analyze with AI" action.
        Args:       rules_result (RulesEngineResult): Phase A output for this trade.
                    trade_context (dict): Raw trade fields (symbol, direction,
                        volume, open/close price, profit, etc.) for the prompt.
                    screenshot_bytes (bytes | None): Raw bytes of a user-uploaded
                        trade screenshot. When present, the vision model
                        (settings.ollama_vision_model) is used instead of the
                        text-only mentor model.
        Returns:    MentorVerdict: Validated verdict/grade/reasoning JSON.
        Raises:     OllamaMentorError: If Ollama is unreachable or returns a
                    payload that doesn't satisfy the MentorVerdict schema.
        """
        schema_hint = (
            "Respond with ONLY a JSON object matching this exact shape: "
            '{"verdict": "good|bad|neutral", "grade": "A|B|C|D|F with optional '
            '+/-", "reasoning": "2-4 sentences", "key_observations": ["short '
            'bullet", "..."]}. Do not include any text outside the JSON object.'
        )
        prompt = (
            "You are a disciplined trading mentor reviewing a closed paper trade.\n"
            f"Trade context: {json.dumps(trade_context, default=str)}\n"
            f"Automated rules-engine assessment: {rules_result.model_dump_json()}\n"
            + ("A chart/terminal screenshot of the trade is attached.\n" if screenshot_bytes else "")
            + schema_hint
        )

        if screenshot_bytes:
            images = [base64.b64encode(screenshot_bytes).decode("ascii")]
            raw = await self._generate_json(settings.ollama_vision_model, prompt, images=images)
        else:
            raw = await self._generate_json(settings.ollama_text_model, prompt)

        try:
            return MentorVerdict.model_validate(raw)
        except Exception as exc:
            raise OllamaMentorError(f"Ollama JSON failed schema validation: {raw!r}") from exc

    async def chat(self, context: str, history: list[dict], message: str) -> str:
        """
        Purpose:    One turn of the conversational trading assistant. Grounds
                    the model in the user's own portfolio/trade/mentor data
                    (so "what did the AI mentor say about my EURUSD trade"
                    style questions are answered from real data, not a guess)
                    while still letting it answer general trading-education
                    questions from its own knowledge. Free-text output —
                    unlike analyze_trade, this is NOT constrained to JSON.
        Args:       context (str): Plain-text summary of the portfolio's
                        trades/evaluations and current live prices (see
                        app/services/chat_context.py).
                    history (list[dict]): Prior turns as
                        [{"role": "user"|"assistant", "content": str}, ...],
                        oldest first.
                    message (str): The user's new message.
        Returns:    str: The assistant's reply text.
        Raises:     OllamaMentorError: If Ollama is unreachable or returns an
                        unexpected response shape.
        """
        system_prompt = (
            "You are the QuantSphere trading assistant, running entirely locally via Ollama — "
            "never mention any other AI provider. You can see the user's own portfolio data "
            "below (trades, rules-engine grades, AI mentor verdicts, live prices); use it to "
            "answer questions about their specific trades or feedback instead of guessing. "
            "You can also answer general trading/market-education questions (indicators, "
            "strategy concepts, terminology, risk management) from your own knowledge — always "
            "make clear that's general information, not financial advice, and that you have no "
            "access to real-time news or data beyond what's listed below. If asked about a trade "
            "you can't find in the data below, say so rather than inventing details. Be concise "
            "and conversational, not a wall of text.\n\n"
            f"=== Portfolio data ===\n{context}"
        )
        messages = [{"role": "system", "content": system_prompt}, *history, {"role": "user", "content": message}]

        payload = {
            "model": settings.ollama_text_model,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": 400},
        }
        try:
            async with httpx.AsyncClient(timeout=settings.ollama_request_timeout_seconds) as client:
                response = await client.post(f"{self._base_url}/api/chat", json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            detail = str(exc) or type(exc).__name__
            raise OllamaMentorError(f"Local Ollama request failed: {detail}") from exc

        reply = response.json().get("message", {}).get("content", "").strip()
        if not reply:
            raise OllamaMentorError("Ollama returned an empty chat response")
        return reply

    async def replay_feedback(self, context: str) -> str:
        """
        Purpose:    On-demand teaching commentary for the Backtest & Replay
                    view — unlike analyze_trade (Phase B), which only sees the
                    trade's own fields, this is grounded in the actual
                    surrounding price action (candles before entry, during the
                    trade, and after exit), so it can comment on real chart
                    context: was the entry timed into a level, overbought/
                    oversold, chasing an extended move, etc. Free-text output,
                    written as a mentor narrating the chart to a student —
                    never auto-triggered, only ever called from an explicit
                    "Ask AI Mentor" click in the replay UI.
        Args:       context (str): Plain-text trade + price-action summary
                        (see app/services/replay_mentor.py).
        Returns:    str: The mentor's teaching commentary.
        Raises:     OllamaMentorError: If Ollama is unreachable or returns an
                        empty response.
        """
        prompt = (
            "You are a trading mentor reviewing a past trade replay with a student, teaching "
            "them how to read the chart around their entry and exit — not just judging the "
            "outcome. Reference concepts like trend/momentum, support/resistance, RSI "
            "overbought/oversold, and price action where relevant to what actually happened. "
            "Be specific to the numbers given, not generic. Structure your answer as: what the "
            "price action looked like leading into the entry, how well-timed the entry was given "
            "that context, what happened during the trade, how the exit looks in hindsight, and "
            "one concrete lesson for next time. Keep it to 4-6 short paragraphs, conversational, "
            "not a rigid template with headers.\n\n"
            f"=== Trade and price-action data ===\n{context}"
        )
        payload = {
            "model": settings.ollama_text_model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 500},
        }
        try:
            async with httpx.AsyncClient(timeout=settings.ollama_request_timeout_seconds) as client:
                response = await client.post(f"{self._base_url}/api/generate", json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            detail = str(exc) or type(exc).__name__
            raise OllamaMentorError(f"Local Ollama request failed: {detail}") from exc

        feedback = response.json().get("response", "").strip()
        if not feedback:
            raise OllamaMentorError("Ollama returned empty replay feedback")
        return feedback

    async def setup_check_narrative(self, quality_result: SetupQualityResult, request_context: dict) -> str:
        """
        Purpose:    On-demand narrative expanding on a "Check My Trade"
                    rule-based setup-quality score — never auto-triggered,
                    only called from an explicit "Explain this score" click,
                    separate from the instant score itself which needs no AI.
                    Must never claim certainty about the trade's outcome.
        Args:       quality_result (SetupQualityResult): The deterministic
                        score/checks already computed by rules_engine.score_trade_setup.
                    request_context (dict): The trader's raw setup-check inputs
                        (symbol, direction, entry/SL/TP, reason for entry, etc.).
        Returns:    str: The narrative commentary.
        Raises:     OllamaMentorError: If Ollama is unreachable or returns an
                        empty response.
        """
        prompt = (
            "You are a trading mentor giving a trader feedback on a setup they are considering, "
            "before they take it. You are given a deterministic rule-based score and checks — "
            "explain what's driving the score in plain language, referencing the specific "
            "strengths and risks below. Do not invent additional checks or claim certainty about "
            "the outcome — this is decision support, not a prediction. If the score is weak, say "
            "so plainly and explain what would make the setup stronger. Keep it to 2-4 short "
            "paragraphs, conversational, not a rigid template with headers.\n\n"
            f"Trader's setup: {json.dumps(request_context, default=str)}\n"
            f"Rule-based assessment: {quality_result.model_dump_json()}"
        )
        payload = {
            "model": settings.ollama_text_model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": 350},
        }
        try:
            async with httpx.AsyncClient(timeout=settings.ollama_request_timeout_seconds) as client:
                response = await client.post(f"{self._base_url}/api/generate", json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            detail = str(exc) or type(exc).__name__
            raise OllamaMentorError(f"Local Ollama request failed: {detail}") from exc

        narrative = response.json().get("response", "").strip()
        if not narrative:
            raise OllamaMentorError("Ollama returned an empty setup-check narrative")
        return narrative

    async def weekly_review_narrative(self, review: WeeklyReviewResult, request_context: dict) -> WeeklyReviewNarrativeResponse:
        """
        Purpose:    Optional, explicitly-triggered AI narrative expanding on a
                    Weekly Review digest — never auto-triggered, only called
                    from an explicit click, and never blocks the base digest
                    (which needs no AI). Must not claim certainty about what
                    next week will bring.
        Args:       review (WeeklyReviewResult): The deterministic weekly digest.
                    request_context (dict): Portfolio id and any extra context.
        Returns:    WeeklyReviewNarrativeResponse: Narrative plus up to 3 focus goals.
        Raises:     OllamaMentorError: If Ollama is unreachable or returns a
                        payload that doesn't satisfy the response schema.
        """
        schema_hint = (
            "Respond with ONLY a JSON object matching this exact shape: "
            '{"narrative": "2-4 sentences summarizing the week", "focus_goals": '
            '["short behavioral goal", "..."]}. focus_goals must have between 1 and 3 items. '
            "Do not include any text outside the JSON object."
        )
        prompt = (
            "You are a trading coach summarizing a trader's real last 7 days of closed trades. "
            "Reference the actual numbers given — do not invent statistics, and do not claim "
            "certainty about what next week will bring. Suggest specific, behavioral (not "
            "outcome-based) focus goals, e.g. about discipline or process, not 'make more profit'.\n\n"
            f"This week's real digest: {review.model_dump_json()}\n"
            f"Context: {json.dumps(request_context, default=str)}\n"
            + schema_hint
        )
        raw = await self._generate_json(settings.ollama_text_model, prompt)
        try:
            return WeeklyReviewNarrativeResponse(narrative=raw["narrative"], focus_goals=raw.get("focus_goals", [])[:3])
        except Exception as exc:
            raise OllamaMentorError(f"Ollama JSON failed schema validation: {raw!r}") from exc

    async def explain_why(self, topic: str, grounding: dict) -> WhyExplanationResponse:
        """
        Purpose:    On-demand, topic-agnostic "why" narrative over an
                    already-computed, deterministic analytics result —
                    generalizes the reasoning/key_observations contract
                    proven by MentorVerdict (Phase B trade grading) to any
                    other analytics module (Trading Health, a Mistake
                    Detector flag, Trader Progression, and future modules)
                    without a bespoke method per module. Never auto-triggered
                    — the caller must have already computed and returned the
                    base result before this is ever called.
        Args:       topic (str): Short machine label echoed back, e.g.
                        "trading_health" or "mistake:overtrading" — included
                        in the prompt so the model stays on-topic.
                    grounding (dict): The real computed numbers to explain.
                        The caller is responsible for recomputing this
                        server-side from real trades immediately before
                        calling — never accept it from the client.
        Returns:    WhyExplanationResponse: Grounded narrative plus key observations.
        Raises:     OllamaMentorError: If Ollama is unreachable or returns a
                        payload that doesn't satisfy the response schema.
        """
        schema_hint = (
            "Respond with ONLY a JSON object matching this exact shape: "
            '{"reasoning": "2-4 sentences explaining why the result came out this way", '
            '"key_observations": ["short specific observation", "..."]}. '
            "key_observations must have between 1 and 4 items. Do not include any text outside the JSON object."
        )
        prompt = (
            "You are explaining WHY a deterministic, rule-based trading-analytics result came out this way. "
            "Reference ONLY the real numbers given below — never invent a statistic that isn't present in the "
            "data. If the data shows no real issue, say so plainly rather than manufacturing a concern. This is "
            "an explanation of the past, not a prediction or financial advice.\n\n"
            f"Topic: {topic}\n"
            f"Real data to explain: {json.dumps(grounding, default=str)}\n"
            + schema_hint
        )
        raw = await self._generate_json(settings.ollama_text_model, prompt)
        try:
            return WhyExplanationResponse(
                topic=topic, reasoning=raw["reasoning"], key_observations=raw.get("key_observations", [])[:4]
            )
        except Exception as exc:
            raise OllamaMentorError(f"Ollama JSON failed schema validation: {raw!r}") from exc
