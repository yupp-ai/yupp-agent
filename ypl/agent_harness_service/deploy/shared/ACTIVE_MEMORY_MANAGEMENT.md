# Active Memory Management

Check **both** memory systems before every task:
```
search_agent_memory(query="<task keywords>", scope="all", agent_name="{your_agent_name}")
```
After tasks, write learnings to private (`agent_memories/` via bash) or shared memory (`store_agent_memory()`). For full docs on memory organization, search parameters, topic hygiene, and recording tool errors, use the `/memory-guide` Claude Code skill.
