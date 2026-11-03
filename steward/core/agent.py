"""The tool-calling loop shared by the CLI and the GUI."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from steward.core.approval import needs_human_approval, scrub_secret
from steward.core.display import summarize_action
from steward.core.errors import Cancelled, StewardError, ToolUnsupportedError
from steward.core.llm.base import ChatMessage, LLMBackend, LLMResponse
from steward.core.llm.fallback import apply_text_tool_calls
from steward.core.models import Agent, ApprovalDecision, ApprovalRequest, ToolCall, TurnResult
from steward.core.prompts import build_system_prompt
from steward.core.settings import Settings
from steward.core.store import Store
from steward.core.tools.base import ToolContext
from steward.core.tools.registry import ToolRegistry


ApprovalHandler = Callable[[ApprovalRequest], ApprovalDecision]
TokenHandler = Callable[[str], None]
SegmentHandler = Callable[[str], None]
ToolHandler = Callable[[object], None]
StatusHandler = Callable[[str], None]


@dataclass
class _TurnSecrets:
    sudo_password: str | None = None


class AgentRunner:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        registry: ToolRegistry,
        backend_factory: Callable[[Settings, Agent], LLMBackend],
    ) -> None:
        self.store = store
        self.settings = settings
        self.registry = registry
        self.backend_factory = backend_factory

    def run_turn(
        self,
        agent_id: str,
        user_text: str,
        *,
        on_token: TokenHandler | None = None,
        on_segment: SegmentHandler | None = None,
        on_tool: ToolHandler | None = None,
        on_status: StatusHandler | None = None,
        approval_handler: ApprovalHandler,
        cancel: threading.Event | None = None,
    ) -> TurnResult:
        text = user_text.strip()
        if not text:
            raise StewardError("Write a message first.")
        agent = self.store.require_agent(agent_id)
        self.store.add_message(agent.id, "user", text)
        notes = self.store.relevant_notes(agent.id, text)
        system = build_system_prompt(agent, notes, self.store.list_machines())
        history = self.store.recent_messages(agent.id, limit=self.settings.history_limit)
        messages: list[ChatMessage] = [ChatMessage("system", system)]
        messages.extend(_to_chat(item) for item in history)

        secrets = _TurnSecrets()
        text_mode = agent.tool_protocol == "text"
        final_text = ""

        for step in range(1, agent.max_steps + 1):
            if _is_cancelled(cancel):
                return TurnResult("", step, cancelled=True)
            if on_status:
                on_status(f"Thinking · step {step} of {agent.max_steps}")
            try:
                response = self._complete(
                    agent, messages, text_mode=text_mode, on_token=on_token, cancel=cancel
                )
            except Cancelled as exc:
                return self._cancelled(agent.id, exc.partial, step, on_segment)
            except ToolUnsupportedError:
                if agent.tool_protocol == "native":
                    raise StewardError(
                        "This model server rejected tool calls. Set the agent's tool protocol to auto or text."
                    ) from None
                text_mode = True
                try:
                    response = self._complete(
                        agent, messages, text_mode=True, on_token=on_token, cancel=cancel
                    )
                except Cancelled as exc:
                    return self._cancelled(agent.id, exc.partial, step, on_segment)

            response = apply_text_tool_calls(response)
            if response.tool_calls:
                self.store.add_message(agent.id, "assistant", response.content, tool_calls=response.tool_calls)
                messages.append(ChatMessage("assistant", response.content, tool_calls=response.tool_calls))
                if response.content and on_segment:
                    on_segment(response.content)
                for call in response.tool_calls:
                    if _is_cancelled(cancel):
                        return TurnResult(response.content, step, cancelled=True)
                    result_text, cleared = self._run_tool(
                        agent,
                        call,
                        secrets,
                        approval_handler=approval_handler,
                        on_tool=on_tool,
                        on_status=on_status,
                        cancel=cancel,
                    )
                    if cleared:
                        secrets.sudo_password = None
                    self.store.add_message(
                        agent.id, "tool", result_text, tool_call_id=call.id, name=call.name
                    )
                    messages.append(
                        ChatMessage("tool", result_text, tool_call_id=call.id, name=call.name)
                    )
                    if _is_cancelled(cancel):
                        return TurnResult(response.content, step, cancelled=True)
                continue

            final_text = response.content.strip() or "(The model returned an empty response.)"
            self.store.add_message(agent.id, "assistant", final_text)
            if on_segment:
                on_segment(final_text)
            return TurnResult(final_text, step)

        notice = (
            "Stopped: reached the maximum number of tool steps for this agent. "
            "Raise max steps in the agent settings if the task needs a longer look."
        )
        self.store.add_message(agent.id, "assistant", notice)
        if on_segment:
            on_segment(notice)
        return TurnResult(notice, agent.max_steps, truncated=True)

    def _complete(
        self,
        agent: Agent,
        messages: list[ChatMessage],
        *,
        text_mode: bool,
        on_token: TokenHandler | None,
        cancel: threading.Event | None,
    ) -> LLMResponse:
        backend = self.backend_factory(self.settings, agent)
        outbound = _flatten_for_text(messages) if text_mode else messages
        tools = None if text_mode else self.registry.schemas()
        return backend.complete(outbound, tools, on_token=on_token, cancel=cancel)

    def _run_tool(
        self,
        agent: Agent,
        call: ToolCall,
        secrets: _TurnSecrets,
        *,
        approval_handler: ApprovalHandler,
        on_tool: ToolHandler | None,
        on_status: StatusHandler | None,
        cancel: threading.Event | None,
    ) -> tuple[str, bool]:
        """Return (result text, whether the sudo password should be forgotten)."""
        tool = self.registry.get(call.name)
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        machine, summary = summarize_action(call.name, arguments)
        if tool is None:
            result = f"Error: unknown tool '{call.name}'. Available tools: {', '.join(self.registry.names())}."
            event = self.store.add_tool_event(
                agent.id, call.name, arguments, result, machine=machine, approved=False, destructive=False
            )
            if on_tool:
                on_tool(event)
            return result, False

        if not call.id:
            call.id = f"call_{uuid.uuid4().hex[:10]}"
        assessment = tool.classify(arguments)
        required, reason, password_only = needs_human_approval(
            agent.approval_mode, tool.approval_class, assessment
        )
        decision = ApprovalDecision(approved=True)
        if required and password_only and secrets.sudo_password:
            required = False
            decision = ApprovalDecision(approved=True, sudo_password=secrets.sudo_password)
        if required:
            if on_status:
                on_status("Waiting for approval")
            request = ApprovalRequest(
                tool_name=tool.name,
                arguments=arguments,
                machine=machine,
                summary=summary,
                reason=reason,
                destructive=assessment.destructive,
                needs_sudo=assessment.needs_sudo,
                password_only=password_only,
            )
            decision = approval_handler(request)
            if decision.sudo_password:
                secrets.sudo_password = decision.sudo_password

        if not decision.approved:
            result = "Denied by the user. Do not retry this exact action unless they ask for it."
            event = self.store.add_tool_event(
                agent.id,
                tool.name,
                arguments,
                result,
                machine=machine,
                approved=False,
                destructive=assessment.destructive,
            )
            if on_tool:
                on_tool(event)
            return result, False

        if on_status:
            on_status(f"Running {tool.name} on {machine}")
        context = ToolContext(
            agent_id=agent.id,
            store=self.store,
            settings=self.settings,
            sudo_password=decision.sudo_password or secrets.sudo_password,
            cancel=cancel,
        )
        try:
            result = tool.execute(arguments, context)
        except Exception as exc:
            result = f"Error: {type(exc).__name__}: {exc}"
        result = scrub_secret(result, context.sudo_password)
        bad_password = _password_rejected(result)
        event = self.store.add_tool_event(
            agent.id,
            tool.name,
            arguments,
            result,
            machine=machine,
            approved=True,
            destructive=assessment.destructive,
        )
        if on_tool:
            on_tool(event)
        return result, bad_password

    def _cancelled(
        self,
        agent_id: str,
        partial: str,
        step: int,
        on_segment: SegmentHandler | None,
    ) -> TurnResult:
        if partial.strip():
            self.store.add_message(agent_id, "assistant", partial)
            if on_segment:
                on_segment(partial)
        return TurnResult(partial, step, cancelled=True)


def _is_cancelled(cancel: threading.Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def _to_chat(message) -> ChatMessage:
    return ChatMessage(
        role=message.role,
        content=message.content,
        tool_calls=message.tool_calls or None,
        tool_call_id=message.tool_call_id,
        name=message.name,
    )


def _flatten_for_text(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Turn native tool messages into plain user/assistant text."""
    flat: list[ChatMessage] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            tags = "\n".join(
                "<tool_call>\n"
                + json.dumps({"name": call.name, "arguments": call.arguments})
                + "\n</tool_call>"
                for call in message.tool_calls
            )
            content = (message.content + "\n" + tags).strip() if message.content else tags
            flat.append(ChatMessage("assistant", content))
        elif message.role == "tool":
            flat.append(ChatMessage("user", f"Tool `{message.name}` returned:\n{message.content}"))
        else:
            flat.append(ChatMessage(message.role, message.content))
    return flat


def _password_rejected(result: str) -> bool:
    lowered = result.lower()
    return "sorry, try again" in lowered or "incorrect password" in lowered or "authentication failure" in lowered
