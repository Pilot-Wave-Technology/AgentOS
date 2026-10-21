# kernel/bridge — localhost bridge between worker and sandbox (E2/ES3 cycle-07).
#
# Direction: the model loop runs in the worker; the worker is the client.
# The listener runs inside the sandbox. Per rule 7, the bridge is localhost
# HTTP on port 9100, shared netns via --network=container:<worker>.
#
# The shim (shim.py) runs inside the sandbox container as its command.
# The client (client.py) runs in the worker and calls the shim.
# tools.py defines the sandbox-side tool implementations.
