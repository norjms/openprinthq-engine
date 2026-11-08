"""Chat agent: ask about and act on the print farm from Slack or Discord.

OpenPrintHQ farm feature. Layout:

- ``config``: settings (rows in the settings table, tokens encrypted at rest, env overrides).
- ``intents``: the fixed set of things the bot can do, and the deterministic parser.
- ``llm``: optional OpenAI-compatible model that maps free-form phrasing onto those intents.
- ``engine_api``: every read and action, made as the linked engine user through the engine's
  own HTTP API in-process, so the API's permission checks apply unchanged.
- ``agent``: the platform-neutral conversation logic (linking, confirmations, audit).
- ``slack_adapter`` / ``discord_adapter``: transports.
- ``manager``: starts and stops the transports from the saved configuration.

See docs/chat-agent.md.
"""
