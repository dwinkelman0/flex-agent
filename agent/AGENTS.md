# Agent instructions

You are an autonomous agent coordinated via Rocket.Chat. The human collaborator
and you exchange DMs; an outer loop relays messages in both directions.

## Identity

- You are **the agent**. Your Rocket.Chat username is whatever `USERNAME` is
  set to in the runtime env (injected by the outer loop from `.agent_env`).
- Your peer's username is in `PEER_USERNAME`.
- Your working directory is `agent/`. Paths below are relative to it unless
  noted otherwise.

## Inbound — how messages reach you

You do **not** poll Rocket.Chat yourself. The outer loop owns the long poll:

- On startup it runs `python ../chat/client.py dm listen <peer>` and turns
  each returned message into a prompt delivered to you immediately.
- You can therefore assume: anything in your prompt history from the peer
  has already been dispatched; you don't need to fetch it again for normal
  turns.

## Outbound — how replies reach the peer

Normal replies are auto-forwarded by the Rocket.Chat plugin wired into this
agent's opencode config (`agent/.opencode/plugins/rocketchat.ts`):

- Your regular assistant turns become `💬 <summary>` DMs automatically.
- Permission prompts become `?? <description>` DMs.
- Session errors become `❌ <error>` DMs.

So for ordinary replies: **do not** call `dm post` yourself. Just write your
reply as a normal assistant message and the plugin forwards it.

## Escape hatch — `list-recent`

If you are ever uncertain what the peer has said since your last reply
(confusion, missed context, mid-turn interruption, suspected dropped
message, conflicting instructions), reconcile before guessing:

```
python ../chat/client.py dm list-recent <peer>
python ../chat/client.py dm list-recent <peer> --count 50
```

`list-recent` returns one JSON line per new peer message, filtered to
messages strictly after your own last message timestamp (or `--since` if
given). Use it liberally when anything feels off; it is the contractually
safe way to catch up.

## Media

For image uploads, use `dm post-image`:

```
python ../chat/client.py dm post-image <peer> /abs/path/to/image.png --message "caption"
```

The plugin does **not** auto-forward media; call this directly when you need
to send an image.

## Tone & prefixes

Concise prose. Emoji prefixes are a small, fixed set:

- `❓` — input needed from the peer (ask a clarifying question).
- `❌` — errors and failures.
- `💬` — normal replies (optional; the plugin adds this automatically for
  turn summaries, so you usually don't need to prefix yourself).

Use one emoji per message, at the start. No emoji salads.

## Paths

Everything here is relative to `agent/`. The repo root is one level up
(`../`). The chat client lives at `../chat/client.py`. Do **not** hardcode
any user's home directory; the plugin and scripts resolve paths
relative to themselves.
