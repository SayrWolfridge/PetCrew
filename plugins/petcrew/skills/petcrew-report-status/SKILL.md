---
name: petcrew-report-status
description: Send an optional explicit semantic milestone to PetCrew only when the user specifically requests exact progress such as 4/10, a custom readable action or result, or explicit named-child reporting. Do not use for routine lifecycle visibility, generic PetCrew mentions, every assistant turn, or ordinary multi-step work; automatic hooks are the primary zero-model-call channel. Russian triggers include отправь точный статус в PetCrew, покажи прогресс 4/10, обнови карточку вручную, передай итог помощника. Do not use it to install or configure PetCrew.
---

# Report status to PetCrew

Use the bundled `petcrew_report_status` MCP tool only for explicitly requested semantic milestones.
Treat reporting as local, observational, and strictly secondary to the user's task. Automatic plugin
hooks own routine start, activity, attention, subagent lifecycle, and completion updates without model
tool calls.

## Invocation gate

- Do not call the reporter merely because PetCrew is installed, open, or mentioned.
- Do not call it on every turn, tool use, plan update, or prose response.
- Do not use it to keep an automatic hook card alive.
- Use it only when the user explicitly asks for semantic detail that hooks cannot carry safely, such
  as a real numeric plan, a custom action label, a named child status, or a concise terminal summary.

## Workflow

1. After the invocation gate is satisfied, report `started` when the task and its first concrete action are known.
2. Report `progress` only when the plan supplies a real `current` and `total`; never invent a fraction.
3. Report `working` after a material change of action, not after every tool call.
4. Report `waiting_input`, `waiting_approval`, or `blocked` only when that state is real.
5. Report `completed`, `failed`, or `cancelled` once, with a short outcome summary.

For multiple agents, report the root and every known child separately. Reuse one stable `agent_id` for each card. Use a canonical subagent id or canonical task path when the runtime exposes one, and pass the root `agent_id` as `parent_agent_id`. Never manufacture a child card when no stable identity is available.

## Field rules

- Keep `task_title` under 120 characters and describe the work, not the user.
- Keep `action` and `progress_label` under 160 characters.
- Keep terminal `summary` under 500 characters and state the result, not a transcript.
- Supply both `current` and `total`, or neither. Require `total > 0` and `0 <= current <= total`.
- Omit `session_id` when the runtime does not expose a stable one; the local reporter will scope events to its server lifetime.
- Never send prompts, tool arguments, tool output, file contents, paths, credentials, tokens, or private conversation text.

## Failure behavior

If the tool is absent, PetCrew is closed, or reporting returns a non-delivery result, continue the main task without retrying, installing anything, editing configuration, or asking the user to repair PetCrew. Mention the reporting gap only when the user explicitly asks whether PetCrew received the update.
