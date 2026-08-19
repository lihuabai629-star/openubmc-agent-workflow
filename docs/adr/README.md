# Architecture Decision Records

Architecture Decision Records capture stable, hard-to-reverse choices for the openUBMC Agent
Workflow. Detailed evidence and alternatives remain in the linked research and arbitration
documents; ADRs define the decisions implementation must preserve.

| ADR | Status | Decision |
| --- | --- | --- |
| [ADR-0001](0001-runtime-core-and-semantic-agent-interface.md) | Accepted | Keep one Runtime Core, a two-operation Agent Interface, and a separate Operator / CI Plane. |
| [ADR-0002](0002-single-run-authority-and-effect-recovery.md) | Accepted | Make RunEngine the sole Run-state writer and treat external Effects as at-least-once with durable recovery. |
| [ADR-0003](0003-turn-gate-artifact-and-distribution-boundaries.md) | Accepted | Return semantic Turns, persist Gate identity, externalize large content, and defer distributed machinery until a real seam exists. |

New ADRs supersede earlier decisions explicitly; they do not silently reinterpret them.
