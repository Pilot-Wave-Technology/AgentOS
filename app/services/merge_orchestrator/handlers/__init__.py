"""Gate handlers — one module per gate_kind.

  pre_integration.py  — resolve target, build local merge preview via gateway,
                         generate merge report, archive to GCS
  integrate.py        — transition to awaiting_approval, write approval card;
                         handle_integrate_approval() executes the push/MR on approval
"""
