"""The chat launch's egress decider (CH-D2, design §3.8.5(o), "open reads, held keys").

Modules are split by owner so no two build sessions share a file. The leaf modules every other one imports:
  parse  one normaliser for a decider request frame (R-1), canonical host, R-2's path rule, the structural walls
         R-2 .. R-4; every policy reads the `ParsedRequest` it returns, never the frame.
  wire   the lines Levain's glue and decider exchange besides srt's own protocol (the observation channel, the
         glue's event file) and the verdict-frame builders.
  guard  the denied-address table srt's resolved-address guard is configured with, generated from
         `levain.broker.front`, and the decider's own address wall.
  lifecycle  C0 read per request: one egress request's state machine (attempt, then outcome XOR
         ended-before-outcome; FAILED when an effect raised), its effects and `settle` (AMBIGUOUS, FAILED and the
         other read-side values).
  records  the `chat_egress_events` rows lifecycle asks for (UNIQUE, the refused-duplicate count `duplicate_rows`,
         the durable FAILED mark), in the dispatch store's `dispatch.db`.
"""
