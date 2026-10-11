"""Sanitized samples of `claude -p --output-format json` output shapes.

These mirror the real event stream from Claude Code 2.1.294 (array of events
beginning with a system/init + a long tools list), but with fake content. The
init/tools dump MUST never be posted to Teams.
"""

# The real-world shape: a JSON ARRAY of events. init first, result last.
ARRAY_OUTPUT = """[
  {"type":"system","subtype":"init","session_id":"sess-abc-123","cwd":"/x",
   "tools":["Read","Edit","Write","Glob","Grep","Bash","WebFetch","TodoWrite"],
   "model":"claude-x","permissionMode":"acceptEdits"},
  {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"작업할게요"}]}},
  {"type":"rate_limit_event","retry_after":0},
  {"type":"result","subtype":"success","is_error":false,
   "result":"테스트 3개 통과했어요.","session_id":"sess-abc-123",
   "num_turns":5,"total_cost_usd":0.1234,"duration_ms":8200,"permission_denials":[]}
]"""

# A single result object (older/alternate shape).
SINGLE_OBJECT = ('{"type":"result","subtype":"success","is_error":false,'
                 '"result":"안녕하세요.","session_id":"sess-9","num_turns":1,'
                 '"total_cost_usd":0.01}')

# JSONL / stream-json: one value per line, trailing noise + a stray control char.
JSONL_OUTPUT = (
    '{"type":"system","subtype":"init","tools":["Read","Bash"],"session_id":"s7"}\n'
    '{"type":"assistant","message":{"content":[{"type":"text","text":"line\x01here"}]}}\n'
    '{"type":"result","subtype":"success","is_error":false,"result":"줄단위 OK",'
    '"session_id":"s7","num_turns":2,"total_cost_usd":0.02}\n'
)

# An error result.
ERROR_OUTPUT = ('[{"type":"system","subtype":"init","tools":["Read"]},'
                '{"type":"result","subtype":"error_max_turns","is_error":true,'
                '"result":null,"session_id":"s-err","num_turns":40}]')

# No result event at all (truncated/garbled).
NO_RESULT = '[{"type":"system","subtype":"init","tools":["Read"]}]'
