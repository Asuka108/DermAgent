/**
 * Harness Conventions
 *
 * Agent-universal conventions (tool-selection priority + secret handling) that
 * every agent this template builds should follow.
 *
 * Injection: appended at the end of the ACP session-prompt branch
 * (`resolveSystemPrompt` when `sessionConfig.systemPrompt` is set), after the
 * local `prompts/flow.base.md` identity and the platform session append. External
 * session prompts do not carry harness conventions, so we append them here.
 *
 * For the non-session path the bundled flow prompt (`prompts/flow.base.md`)
 * already includes equivalent rules; this constant covers the ACP session branch.
 */

/** Harness-level rules injected into ACP-delivered prompts. Kept small — not agent identity. */
export const PLATFORM_CONVENTIONS = `\
## Tool Selection Priority (MANDATORY)
1. MCP tools — use the bound native MCP tools (loaded from configured MCP servers) first.
2. Built-in tools — \`http_request\`, \`json_utils\`, bash / filesystem / search.
3. deepagents built-in tools — \`read_file\`, \`write_file\`, \`edit_file\`, \`execute\`, \`task\`.
4. Write custom code — only when no existing tool fits the need.

## Secrets
- Never hardcode API keys or secrets. Use environment variables or ask the user to configure them.`;
