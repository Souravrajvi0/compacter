# Compaction benchmark results

## Fixtures: naive vs ctxpipe

| fixture | strategy | tokens before -> after | pins preserved | secret leaked | pair splits |
|---|---|---|---|---|---|
| session-a.jsonl | naive | 778 -> 796 | no | **YES** | 0 |
| session-a.jsonl | ctxpipe | 778 -> 178 | yes | no | 0 |
| session-b.jsonl | naive | 69 -> 75 | yes | no | 0 |
| session-b.jsonl | ctxpipe | 69 -> 57 | yes | no | 0 |
| original.jsonl | naive | 778 -> 796 | no | **YES** | 0 |
| original.jsonl | ctxpipe | 778 -> 178 | yes | no | 0 |

## Tool-heavy session (drop_result_if: no_errors + truncation)

- tokens: 1261 -> 121
- error result preserved: True
- successful results dropped: 6

## Chained compaction (3 rounds on session-a)

- round 1: valid_json=True goals_in_high=1 tokens_after=178 goals_header_count=0
- round 2: valid_json=True goals_in_high=1 tokens_after=345 goals_header_count=0
- round 3: valid_json=True goals_in_high=1 tokens_after=505 goals_header_count=0

## Profile comparison (session-a, keep_recent=4)

| profile | tokens after | high chars | medium chars | low chars |
|---|---|---|---|---|
| conservative | 178 | 313 | 87 | 87 |
| aggressive | 151 | 313 | 87 | 0 |
| code-heavy | 178 | 313 | 87 | 87 |
