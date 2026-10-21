"""Stage L — what the goal knows.

`goal_state` is the board; this is why the board looks like that.

The record is deliberately thin, because most of it already existed:

  decisions   entries inside APPROVED PLANS (`decisions.py` reads them;
              `register_plan` has been storing them all along)
  evidence    a join over rows that exist — branches, landings, paths,
              artifacts, resources, checks
  knowledge   `stories.knowledge`, one entry per plan cycle: what the
              work taught, which is the only part nothing recorded
  alignment   `kernel.alignment_flags`, the one new table
"""
from kernel.knowledge.alignment import (open_flags, raise_alignment,
                                        resolve_alignment)
from kernel.knowledge.read import goal_knowledge, plans_for
from kernel.knowledge.write import record_cycle, record_finding

__all__ = ["goal_knowledge", "plans_for", "record_finding", "record_cycle",
           "raise_alignment", "resolve_alignment", "open_flags"]
