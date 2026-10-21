# AgentOS VS Code extension

`agentos-vscode-<version>.vsix` in this folder is the packaged **AgentOS Local
Execution** extension. It lets a teammate work an AgentOS coding story in their
own VS Code, while your AgentOS deployment remains the system of record.
Licensed under Apache-2.0, like the rest of AgentOS.

## For the person deploying AgentOS
Make it available from your deployment. This is step 7.5 of the platform guide
([`agent_os_platform/deploy/07-build-and-deploy.md`](https://github.com/Pilot-Wave-Technology/AgentOS/blob/agent_os_platform/deploy/07-build-and-deploy.md)):

```bash
curl -L -o ~/Downloads/agentos-vscode-<version>.vsix \
  https://github.com/Pilot-Wave-Technology/AgentOS/raw/main/extension/agentos-vscode-<version>.vsix
./publish-extension.sh ~/Downloads/agentos-vscode-<version>.vsix "Initial release"
```

Your deployment's **VS Code** page (`/extension`) then offers the download, and
installed copies check it for updates once a day.

## For a teammate installing it
Download it from your deployment's **VS Code** page (or use this file), then:

- **VS Code:** Extensions view → `⋯` menu → **Install from VSIX…** → pick the
  file → **Reload Window**.
- **CLI:** `code --install-extension agentos-vscode-<version>.vsix`

On first run it asks for your control plane URL (your admin gives you this) and
signs you in through your browser.

**Needs on your `PATH`:** VS Code 1.85+, Git, Node.js 18+, and the Claude Code
CLI (or Codex), installed and signed in. Run **AgentOS: Check Coding CLI** to
confirm.
