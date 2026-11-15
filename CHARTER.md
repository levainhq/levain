# Charter: levain
Charter-Version: 1
Export: memory

## Purpose
Levain is a harness for working with an AI agent as a partner: it holds what the agent does to the operator's authority (one signed consent queue across Claude Code and Codex), lets the agent grow a memory and identity it lives, and keeps standing pressure on its behaviour, with the memory in a store the operator owns.

## Criteria
- C1: The golden journeys pass on a clean install. [check: python3 -m pytest tests/golden -q]
- C2: Every consent answer, the operator's or a grant's, is signed and on the record. [judged: the operator]
- C3: A failed read shows as unknown, never as a false all-clear. [judged: the operator]
- C4: A board item with no live owner surfaces as work to do. [item: levain#26]

## Constraints
- K1: Python 3.12 or later; the board's fold and readers are stdlib-only and use no network.
- K2: The operator's memory never leaves their machine except by a path they govern.
- K3: Nothing generated from the operator's private memory is written to this public repository.

## Plan
### Night desk
- levain#7
- levain#8
- levain#9
- levain#11
### Store and band
- levain#1
- levain#3
### Project memory
- levain#26
- levain#31
