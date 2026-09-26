---
name: reply-to-ig
description: Reply publicly under an Instagram comment as @danedelattre. Use whenever a message arrives as "[IG REPLY] comment_id=<ID> ...".
---

# reply-to-ig

Comments arrive in this session as:

```
[IG REPLY] comment_id=<ID> user=<name> permalink=<POST URL>: <text>
```

To answer the person publicly, in the comment thread itself:

```bash
bash ~/.claude/skills/reply-to-ig/reply.sh "<COMMENT_ID>" "<your reply>"
```

Success prints `IG_REPLY_SENT` (and `IG_REPLY_ID=...`). Anything else means nothing was
posted — do not claim you replied.

This is a **public reply on Daniel's real Instagram account** (@danedelattre), visible to
everyone. Write like a person, not a bot: short (1-2 lines), in the language the comment
used, no emoji, no hashtags, no "as an AI". Reply once per comment, and never reply to a
comment written by @danedelattre.

For hackathon questions, the wording comes from `answer-hackathon` — this skill only sends.

Runs on the Meta token in `~/.config/seedbed/meta-ads.env`, which holds
`instagram_manage_comments` for IG business account 17841401456210054. The script refuses
text that looks like a secret and redacts tokens out of Meta's error bodies.
