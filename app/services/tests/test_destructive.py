"""Ec3a — which shell commands cannot be undone.

The old list was sixteen substrings matched against a lowercased command.
It caught the classic ways to destroy a MACHINE and missed every way to
destroy an ACCOUNT: `terraform apply`, `aws s3 rm`, `gcloud … delete`,
`helm uninstall`, `gh pr merge` all classified `reversible` and ran
silently. Those are exactly what an errand with a cloud credential reaches
for.

BOTH DIRECTIONS ARE THE TEST. A longer substring list would catch more and
also fire on `echo "terraform apply"`, `truncate --help` and
`grep -r "drop table" .` — and the module this serves is right that an
over-eager list gets switched off:

    Narrow on purpose: an over-eager list turns every run into an approval
    queue and gets switched off, which is worse than not having one.

So the false-positive table below is not padding. It is half the
specification, and every entry is a command somebody runs in an ordinary
hour of work.
"""
from __future__ import annotations

import pytest

from kernel.harness.destructive import is_destructive, reason
from kernel.harness.effects import IRREVERSIBLE, REVERSIBLE, classify

# ── what must be caught ──────────────────────────────────────────────────

CAUGHT = [
    # The machine, kept from the original list.
    ("rm -rf /", "filesystem"),
    ("mkfs.ext4 /dev/sda1", "formats"),
    ("dd if=/dev/zero of=/dev/sda", "raw blocks"),
    ("shutdown -h now", "stops"),
    # Infrastructure as code — none of these were caught before.
    ("terraform apply", "infrastructure"),
    ("terraform apply -auto-approve", "infrastructure"),
    ("sudo terraform apply", "infrastructure"),
    ("terraform -chdir=infra apply", "infrastructure"),
    ("terraform destroy", "infrastructure"),
    ("cd infra && terraform apply", "infrastructure"),
    ("pulumi up --yes", "infrastructure"),
    ("tofu destroy", "infrastructure"),
    # Kubernetes and Helm.
    ("kubectl delete deployment api", "cluster"),
    ("kubectl drain node-1", "evicts"),
    ("kubectl apply -f prod.yaml", "live cluster"),
    ("helm uninstall prod", "release"),
    ("helm upgrade api ./chart", "release"),
    # AWS.
    ("aws s3 rm s3://bucket/key", "bucket"),
    ("aws --profile prod s3 rm s3://b --recursive", "bucket"),
    ("aws s3 rb s3://bucket", "bucket"),
    ("aws s3 sync . s3://b --delete", "deletes"),
    ("aws ec2 terminate-instances --instance-ids i-1", "cloud resource"),
    ("aws rds delete-db-instance --db-instance-identifier x", "cloud"),
    ("aws iam remove-user-from-group --user-name u", "cloud"),
    # Google Cloud and Azure.
    ("gcloud run services delete api", "cloud resource"),
    ("gcloud compute instances delete vm-1 --zone z", "cloud resource"),
    ("gsutil rm gs://bucket/object", "bucket"),
    ("bq rm -t dataset.table", "dataset"),
    ("az group delete --name rg", "cloud resource"),
    # Databases, inside a client.
    ('psql -c "drop table users"', "table"),
    ('psql "$DB" -c "DELETE FROM users WHERE 1=1"', "rows"),
    ('mysql -e "truncate table sessions"', "empties"),
    ('sqlite3 app.db "drop table t"', "table"),
    ('psql -c "UPDATE users SET active = false"', "every row"),
    # Git, GitHub, registries.
    ("git push origin main", "publishes"),
    ("git push --force", "publishes"),
    ("gh pr merge 12 --squash", "pull request"),
    ("gh repo delete owner/name", "repository"),
    ("docker push registry/img:1", "publishes"),
    ("npm publish", "package"),
    ("twine upload dist/*", "package"),
    # Sending.
    ("curl -X POST https://api.example.com/send", "remote"),
    ("curl --request DELETE https://api/x", "remote"),
    ("curl --request=PUT https://api/x", "remote"),
    ("sendmail -t < message.txt", "mail"),
]

# ── what must NOT be caught ──────────────────────────────────────────────
#
# Ordinary work. Every false positive here would be a person's attention
# spent on nothing, and the third one teaches everybody to approve
# without reading.

SPARED = [
    "ls -la", "pwd", "cat README.md", "pytest -q", "python -m pytest",
    # The read-only halves of the same tools.
    "terraform plan", "terraform validate", "terraform fmt",
    "kubectl get pods", "kubectl logs api-1", "kubectl describe pod x",
    "helm list", "helm template ./chart",
    "aws s3 ls s3://bucket", "aws s3 cp local s3://b/k",
    "aws ec2 describe-instances", "gcloud run services list",
    "gcloud projects describe p", "az account show", "gsutil ls gs://b",
    "git status", "git pull", "git log --oneline", "git diff",
    "gh pr view 12", "gh pr list",
    "docker build -t x .", "docker ps", "docker logs c1",
    "npm install", "npm run build", "npm test", "pip install -r req.txt",
    # Reads that MENTION a destructive phrase.
    'echo "terraform apply"',
    'grep -r "drop table" migrations/',
    'rg "delete from" src/',
    "cat migrations/drop_table_users.sql",
    "ls migrations/ | grep delete",
    "truncate --help",
    'psql -c "select * from users"',
    'psql -c "update users set x = 1 where id = 2"',
    "curl https://api.example.com/status",
    "curl -s -o out.json https://api/x",
    "curl -H 'Accept: application/json' https://api/x",
    "python scripts/report.py --dry-run",
]


@pytest.mark.parametrize("command,fragment", CAUGHT,
                         ids=[c[:44] for c, _ in CAUGHT])
def test_it_is_caught_and_says_why(command, fragment):
    """The REASON, not just a boolean. It goes on the card a person reads,
    and "this needs approval" tells them nothing they can decide with."""
    said = reason(command)
    assert said, f"{command!r} runs silently"
    assert fragment in said, f"{command!r} -> {said!r}"


@pytest.mark.parametrize("command", SPARED, ids=[c[:44] for c in SPARED])
def test_ordinary_work_is_not_an_approval_queue(command):
    assert not is_destructive(command), (
        f"{command!r} would park the run and spend a person's attention "
        f"on nothing")


def test_the_classifier_uses_it():
    """The join. A matcher nothing calls protects nothing."""
    assert classify("bash", {"command": "terraform apply"}) == IRREVERSIBLE
    assert classify("bash", {"command": "terraform plan"}) == REVERSIBLE
    assert classify("bash_background",
                    {"command": "aws s3 rm s3://b"}) == IRREVERSIBLE


def test_a_second_command_after_a_safe_one_is_still_seen():
    """A substring match on the whole line cannot see two commands, and
    `ls && terraform apply` is the shape that hides one."""
    for chained in ("ls && terraform apply",
                    "make build; helm uninstall prod",
                    "cat x | psql -c 'drop table t'",
                    "test -f x || aws s3 rb s3://b"):
        assert is_destructive(chained), chained


def test_an_empty_or_missing_command_is_not_destructive():
    """A malformed call must not park the run — it is refused elsewhere,
    and treating "" as dangerous would make every argument error a
    question for a person."""
    for empty in ("", None, "   "):
        assert not is_destructive(empty)


def test_what_it_cannot_see_is_documented_not_pretended():
    """A speed bump on the obvious road, and the module says so. Claiming
    more would be worse than claiming less, because somebody would rely
    on it — the sandbox is the container, and Ec4 owns egress."""
    import io
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    src = io.open(root / "kernel/harness/destructive.py",
                  encoding="utf-8").read()
    assert "cannot see" in src.lower()
    # And it really cannot: named so nobody discovers it as a surprise.
    assert not is_destructive('bash -c "terraform apply"')


def test_the_ceiling_is_still_not_configurable():
    """Widening what counts as irreversible is only safe while the class
    itself cannot be loosened. Six adversarial policies, as H2 promised."""
    from kernel.harness.effects import requires_approval

    for policy in ({}, None, {"approve_reversible": True},
                   {"approve_contained": True},
                   {"approve_irreversible": True},
                   {"approve_reversible": True, "approve_contained": True}):
        assert requires_approval(IRREVERSIBLE, policy) is True, policy
