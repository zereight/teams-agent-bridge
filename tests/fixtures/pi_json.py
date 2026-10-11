"""Sanitized `pi --mode json` JSONL output (strict JSONL, one object per line).

Mirrors the shape in pi 1.1.0 docs/json.md: a `session` header, then agent/turn/
message/tool events. The final assistant `message_end` carries the answer and a
cumulative `usage.cost.total`.
"""

PI_JSON_OK = "\n".join([
    '{"type":"session","version":3,"id":"bridge-dev","timestamp":"2026-10-11T04:00:00.000Z","cwd":"/wt"}',
    '{"type":"agent_start"}',
    '{"type":"turn_start"}',
    '{"type":"message_start","message":{"role":"user","content":"name 값만 알려줘"}}',
    '{"type":"message_end","message":{"role":"user","content":"name 값만 알려줘"}}',
    '{"type":"message_start","message":{"role":"assistant","content":[],"stopReason":"pending"}}',
    '{"type":"message_update","usage":{"input":100,"output":5,"cost":{"total":0.004}},"assistantMessageEvent":{"type":"text_delta","contentIndex":0,"delta":"name 은 "}}',
    '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"name 값은 example-app 입니다.\\n\\n확인: package.json 1번째 줄에서 읽었습니다."}],"usage":{"input":100,"output":20,"cost":{"total":0.012}}}}',
    '{"type":"turn_end","message":{"role":"assistant"},"toolResults":[]}',
    '{"type":"agent_end","messages":[],"willRetry":false}',
    '{"type":"agent_settled","aborted":false}',
])

# print mode (-p): just the final assistant text, no JSON
PI_PRINT_OK = "name 값은 example-app 입니다."
