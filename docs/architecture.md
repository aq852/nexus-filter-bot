# Architecture

The bot uses a single, owner-controlled index. Source channels contain files that the owner is authorized to distribute. A group member sends a search query, and the bot returns matching records from that central index inside the group.

```text
Approved source channels -> indexer -> MongoDB library -> Telegram bot -> public groups
```

Group administrators may add the bot to their group, but cannot alter the shared library, owner controls, or global policies. The owner can optionally disable searching in individual groups.
