"""G1 — the fixture repository the corpus is graded against.

Not the toy Counter. A task graded on a five-file tree measures nothing
about orientation, which is the executor behaviour Stage G exists to
measure: whether it searches before reading, reads bounded ranges, and
finds one function without pulling three whole files into context.

So the fixture has the properties that make finding things HARD in the
way a real repository makes them hard:

  DECOYS       `legacy/` holds an older implementation with similar
               names. Grepping for the obvious symbol finds the wrong
               one first.
  DEPTH        the target is never in the file the task names.
  SIZE         one module is long enough that reading it whole is a
               measurable cost against reading a range.
  A REAL SUITE the existing tests pass, so a task that breaks one is
               caught by the task's own criteria rather than by
               inspection.

Generated rather than committed, because a fixture in the tree is a
fixture that drifts with the tree — and the corpus must mean the same
thing in six months. One function, deterministic, no randomness.
"""

from __future__ import annotations

import os
import subprocess

FILES: dict[str, str] = {}


def _add(path: str, body: str) -> None:
    FILES[path] = body


_add("README.md", """# ledger

A small double-entry ledger. Accounts hold entries; a book holds
accounts and can report balances.

    from ledger.book import Book

Layout:
  ledger/       the library
  legacy/       the previous implementation, kept for migration only
  tests/        the suite
""")

_add("ledger/__init__.py", "")

_add("ledger/money.py", '''"""Amounts, in minor units. Never floats."""


class Money:
    def __init__(self, minor: int, currency: str = "GBP"):
        if not isinstance(minor, int):
            raise TypeError("Money takes minor units as an int")
        self.minor = minor
        self.currency = currency

    def __add__(self, other):
        self._same(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other):
        self._same(other)
        return Money(self.minor - other.minor, self.currency)

    def __eq__(self, other):
        return (isinstance(other, Money) and self.minor == other.minor
                and self.currency == other.currency)

    def __repr__(self):
        return f"Money({self.minor}, {self.currency!r})"

    def _same(self, other):
        if not isinstance(other, Money):
            raise TypeError("Money only combines with Money")
        if other.currency != self.currency:
            raise ValueError(
                f"cannot combine {self.currency} with {other.currency}")
''')

_add("ledger/entry.py", '''"""One movement against one account."""

from ledger.money import Money


class Entry:
    def __init__(self, description: str, amount: Money):
        self.description = description
        self.amount = amount

    def __repr__(self):
        return f"Entry({self.description!r}, {self.amount!r})"
''')

_add("ledger/account.py", '''"""An account is a name and the entries posted to it."""

from ledger.entry import Entry
from ledger.money import Money


class Account:
    def __init__(self, name: str, currency: str = "GBP"):
        self.name = name
        self.currency = currency
        self.entries: list[Entry] = []

    def post(self, description: str, amount: Money) -> Entry:
        if amount.currency != self.currency:
            raise ValueError(
                f"account {self.name} is {self.currency}, "
                f"not {amount.currency}")
        entry = Entry(description, amount)
        self.entries.append(entry)
        return entry

    def balance(self) -> Money:
        total = Money(0, self.currency)
        for entry in self.entries:
            total = total + entry.amount
        return total
''')

_add("ledger/book.py", '''"""A book holds accounts and reports on them."""

from ledger.account import Account
from ledger.money import Money


class Book:
    def __init__(self, currency: str = "GBP"):
        self.currency = currency
        self.accounts: dict[str, Account] = {}

    def open_account(self, name: str) -> Account:
        if name in self.accounts:
            raise ValueError(f"account {name} already exists")
        account = Account(name, self.currency)
        self.accounts[name] = account
        return account

    def account(self, name: str) -> Account:
        if name not in self.accounts:
            raise KeyError(f"no account named {name}")
        return self.accounts[name]

    def total(self) -> Money:
        running = Money(0, self.currency)
        for account in self.accounts.values():
            running = running + account.balance()
        return running

    def report(self) -> list[tuple]:
        """Every account and its balance, sorted by name."""
        return [(name, self.accounts[name].balance())
                for name in sorted(self.accounts)]
''')

# The decoy. Similar names, wrong implementation, kept "for migration".
_add("legacy/__init__.py", "")
_add("legacy/accounts.py", '''"""DEPRECATED. The pre-1.0 ledger. Do not extend.

Kept because two downstream systems still import it. `LegacyAccount`
looks like `ledger.account.Account` and is not: amounts are floats and
balances round.
"""


class LegacyAccount:
    def __init__(self, name):
        self.name = name
        self.rows = []

    def post(self, description, amount):
        self.rows.append((description, float(amount)))

    def balance(self):
        return round(sum(a for _, a in self.rows), 2)


class LegacyBook:
    def __init__(self):
        self.accounts = {}

    def open_account(self, name):
        self.accounts[name] = LegacyAccount(name)
        return self.accounts[name]

    def total(self):
        return round(sum(a.balance() for a in self.accounts.values()), 2)
''')

_add("tests/__init__.py", "")
_add("tests/test_money.py", '''from ledger.money import Money


def test_add():
    assert Money(100) + Money(250) == Money(350)


def test_subtract():
    assert Money(500) - Money(200) == Money(300)


def test_currency_mismatch_refused():
    try:
        Money(100, "GBP") + Money(100, "USD")
    except ValueError:
        return
    raise AssertionError("mixing currencies should raise")
''')

_add("tests/test_book.py", '''from ledger.book import Book
from ledger.money import Money


def test_open_and_post():
    book = Book()
    account = book.open_account("cash")
    account.post("opening", Money(1000))
    assert book.account("cash").balance() == Money(1000)


def test_total_across_accounts():
    book = Book()
    book.open_account("cash").post("in", Money(1000))
    book.open_account("bank").post("in", Money(2500))
    assert book.total() == Money(3500)


def test_report_is_sorted():
    book = Book()
    for name in ("zebra", "apple", "mango"):
        book.open_account(name)
    assert [n for n, _ in book.report()] == ["apple", "mango", "zebra"]
''')


def seed(path: str) -> str:
    """Write the fixture and commit it. Returns the commit sha."""
    subprocess.run(["rm", "-rf", path], capture_output=True)
    for rel, body in FILES.items():
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(body)

    def git(*args):
        result = subprocess.run(["git", "-C", path, *args],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"git {args[0]}: {result.stderr.strip()[:300]}")
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "corpus@agentos.local")
    git("config", "user.name", "Corpus Fixture")
    git("add", "-A")
    git("commit", "-q", "-m", "ledger fixture")
    return git("rev-parse", "HEAD")
