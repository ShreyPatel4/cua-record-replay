"""Synthetic seed data: 15 invented members and an in-memory sub-account ledger.

No real names or numbers. Member 10013 is restricted so the permission-denied path exists naturally.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from decimal import Decimal

RESTRICTED_MEMBER_ID = "10013"

ACCOUNT_TYPES: dict[str, str] = {
    "CERT": "Share Certificate",
    "MMKT": "Money Market",
    "HOLI": "Holiday Club",
}


@dataclass(frozen=True)
class Member:
    member_id: str
    name: str
    joined: str
    branch: str
    savings: Decimal
    checking: Decimal
    restricted: bool = False

    @property
    def status(self) -> str:
        return "Restricted" if self.restricted else "Active"


_SEED: list[tuple[str, str, str, str, str]] = [
    # name, joined, branch, savings, checking
    ("Avery Quillfeather", "2009-02-17", "Maple St", "2140.18", "388.02"),
    ("Brook Lantern", "2011-06-03", "Harbor", "15.00", "1204.55"),
    ("Casey Marrowind", "2014-11-22", "Maple St", "9870.40", "45.10"),
    ("Dana Thistlecomb", "2003-01-09", "Uptown", "301.77", "3310.00"),
    ("Emery Pondwick", "2019-08-30", "Harbor", "0.00", "72.64"),
    ("Finley Oakbarrow", "2016-04-12", "Uptown", "44210.93", "5120.33"),
    ("Gray Wrenfield", "2012-09-05", "Maple St", "4210.55", "1002.10"),
    ("Harper Coldbrook", "2021-12-01", "Harbor", "610.00", "18.25"),
    ("Indigo Sallowmere", "2007-03-28", "Uptown", "1288.46", "940.00"),
    ("Jules Brambleton", "2018-07-19", "Maple St", "77.31", "2051.87"),
    ("Kai Fernhollow", "2015-05-14", "Harbor", "3505.00", "600.40"),
    ("Lane Copperfen", "2010-10-10", "Uptown", "12.34", "0.99"),
    ("Morgan Ashgrove", "2005-02-02", "Maple St", "88000.00", "12000.00"),
    ("Noel Driftwillow", "2022-03-15", "Harbor", "250.00", "250.00"),
    ("Oakley Pebblestone", "2013-01-23", "Uptown", "5432.10", "123.45"),
]


def _build_members() -> dict[str, Member]:
    members: dict[str, Member] = {}
    for offset, (name, joined, branch, savings, checking) in enumerate(_SEED, start=1):
        member_id = str(10000 + offset)
        members[member_id] = Member(
            member_id=member_id,
            name=name,
            joined=joined,
            branch=branch,
            savings=Decimal(savings),
            checking=Decimal(checking),
            restricted=member_id == RESTRICTED_MEMBER_ID,
        )
    return members


@dataclass(frozen=True)
class SubAccount:
    number: str
    member_id: str
    account_type: str
    nickname: str
    initial_deposit: Decimal


@dataclass
class Ledger:
    """Members are immutable seed data; sub-accounts are the only thing the app writes."""

    members: dict[str, Member] = field(default_factory=_build_members)
    subaccounts: dict[str, SubAccount] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def open_subaccount(
        self, member_id: str, account_type: str, nickname: str, deposit: Decimal
    ) -> SubAccount:
        with self._lock:
            sequence = sum(1 for s in self.subaccounts.values() if s.member_id == member_id) + 1
            number = f"{member_id}-S{sequence:02d}"
            sub = SubAccount(number, member_id, account_type, nickname, deposit)
            self.subaccounts[number] = sub
            return sub

    def reset(self) -> None:
        with self._lock:
            self.subaccounts.clear()


def money(value: Decimal) -> str:
    return f"${value:,.2f}"
