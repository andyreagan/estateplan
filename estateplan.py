#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["flask>=3.0", "reportlab>=4.0"]
# ///
"""
estateplan.py - single-file estate document generator.

Stores people and choices in a SQLite database and rebuilds the four
Trust & Will style documents (Last Will & Testament, Durable Power of
Attorney, Advance Health Care Directive, HIPAA Authorization) for each
principal from that data.

Usage:
    uv run estateplan.py                 # web UI on http://127.0.0.1:5077
    uv run estateplan.py serve --port N
    uv run estateplan.py build [--out DIR]
    uv run estateplan.py text PRINCIPAL DOC   # plain text of one document
    uv run estateplan.py verify ORIGINALS_DIR # sentence diff vs originals
    uv run estateplan.py export --out family.local.json   # save real data (gitignored)
    uv run estateplan.py import family.local.json         # load it back
    uv run estateplan.py reseed          # wipe DB; reload family.local.json or the demo Doe family

The database lives next to this file as estateplan.db.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "estateplan.db"
OUT_DIR = HERE / "output"

DOCS = {
    "will": "Last Will & Testament",
    "poa": "Power of Attorney",
    "ahcd": "Advance Health Care Directive",
    "hipaa": "HIPAA Authorization",
}

ROLES = [
    ("child", "Children", "Listed in the will; order is birth order."),
    ("guardian", "Guardians", "Will: primary first, then backups in order."),
    ("executor", "Executors", "Will: primary first, then backups. Executor is also Digital Executor."),
    ("health_agent", "Health care agents", "Directive: primary first, then alternates."),
    ("hipaa_recipient", "HIPAA recipients", "Authorization: everyone who may receive medical records."),
    ("poa_agent", "POA agents", "Power of attorney: up to two co-agents (both must act together if two)."),
    ("poa_backup", "POA backup agents", "Power of attorney: successor agents, in order, up to three."),
]
ROLE_LABEL = {r[0]: r[1] for r in ROLES}

NUMBER_WORDS = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS person (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    address TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    birth_date TEXT DEFAULT '',
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS principal (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id),
    spouse_id INTEGER REFERENCES person(id),
    state_name TEXT DEFAULT 'Massachusetts',
    notary_jurisdiction TEXT DEFAULT 'Commonwealth of Massachusetts',
    poa_statute TEXT DEFAULT 'M.G.L. ch 190B §5-501 et seq.',
    residence_line TEXT DEFAULT '',
    remains TEXT DEFAULT 'cremated',
    ceremony TEXT DEFAULT 'executor',
    final_special_request TEXT DEFAULT '',
    care_preference TEXT DEFAULT 'improve_only',
    organ_donation INTEGER DEFAULT 1,
    hc_special_instructions TEXT DEFAULT '',
    poa_special_instructions TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS role (
    id INTEGER PRIMARY KEY,
    principal_id INTEGER NOT NULL REFERENCES principal(id),
    role TEXT NOT NULL,
    person_id INTEGER NOT NULL REFERENCES person(id),
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS gift (
    id INTEGER PRIMARY KEY,
    principal_id INTEGER NOT NULL REFERENCES principal(id),
    recipient_id INTEGER NOT NULL REFERENCES person(id),
    item TEXT NOT NULL,
    position INTEGER NOT NULL
);
"""

SEED_FILE = HERE / "family.local.json"   # gitignored; created by `export`, loaded on first run if present

# Built-in seed: a fictional family so the script runs (and demos) without any
# real data. Real data lives in family.local.json (see export / import).
DOE_HOME = "123 Main St\nSpringfield\nMassachusetts, 01101"
SEED_DATA = {
    "people": [
        {"full_name": "Jane Doe", "address": DOE_HOME, "notes": "principal"},
        {"full_name": "John Doe", "address": DOE_HOME, "notes": "principal"},
        {"full_name": "Emma Doe", "birth_date": "2016-03-14", "notes": "child"},
        {"full_name": "Liam Doe", "birth_date": "2018-09-02", "notes": "child"},
        {"full_name": "Mary Doe", "address": "45 Elm St, Worcester MA 01602", "phone": "4135550100",
         "email": "mary.doe@example.com", "notes": "Jane's mother"},
        {"full_name": "Patricia Roe", "address": "9 Oak Ave, Albany NY 12203", "phone": "5185550101",
         "email": "patricia.roe@example.com", "notes": "John's mother"},
        {"full_name": "Kevin Doe", "notes": "Jane's brother"},
    ],
    "principals": [
        {
            "person": "Jane Doe", "spouse": "John Doe",
            "residence_line": "123 Main St, Springfield, MA, 01101",
            "roles": {
                "child": ["Emma Doe", "Liam Doe"],
                "guardian": ["John Doe", "Mary Doe", "Patricia Roe"],
                "executor": ["John Doe", "Mary Doe", "Patricia Roe"],
                "health_agent": ["John Doe", "Mary Doe", "Patricia Roe"],
                "hipaa_recipient": ["John Doe", "Mary Doe", "Patricia Roe"],
                "poa_agent": ["John Doe"],
                "poa_backup": ["Mary Doe", "Patricia Roe"],
            },
            "gifts": [{"recipient": "Kevin Doe", "item": "Record collection"}],
        },
        {
            "person": "John Doe", "spouse": "Jane Doe",
            "residence_line": "123 Main St, Springfield, MA, 01101",
            "roles": {
                "child": ["Emma Doe", "Liam Doe"],
                "guardian": ["Jane Doe", "Patricia Roe", "Mary Doe"],
                "executor": ["Jane Doe", "Patricia Roe", "Mary Doe"],
                "health_agent": ["Jane Doe", "Patricia Roe", "Mary Doe"],
                "hipaa_recipient": ["Jane Doe", "Patricia Roe", "Mary Doe"],
                "poa_agent": ["Jane Doe"],
                "poa_backup": ["Patricia Roe", "Mary Doe"],
            },
            "gifts": [],
        },
    ],
}

PERSON_FIELDS = ("full_name", "address", "phone", "email", "birth_date", "notes")
PRINCIPAL_FIELDS = ("state_name", "notary_jurisdiction", "poa_statute", "residence_line", "remains", "ceremony",
                    "final_special_request", "care_preference", "organ_donation", "hc_special_instructions",
                    "poa_special_instructions")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    if con.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 0:
        if SEED_FILE.exists():
            load_data(con, json.loads(SEED_FILE.read_text()))
        else:
            load_data(con, SEED_DATA)
    return con


def load_data(con: sqlite3.Connection, data: dict) -> None:
    """Replace the database contents with `data` (the export/import format)."""
    for table in ("gift", "role", "principal", "person"):
        con.execute(f"DELETE FROM {table}")
    ids: dict[str, int] = {}
    for person in data["people"]:
        row = {k: person.get(k, "") for k in PERSON_FIELDS}
        cur = con.execute(
            "INSERT INTO person(full_name,address,phone,email,birth_date,notes) VALUES (?,?,?,?,?,?)",
            tuple(row[k] for k in PERSON_FIELDS),
        )
        ids[row["full_name"]] = cur.lastrowid
    for pr in data["principals"]:
        fields = {k: pr[k] for k in PRINCIPAL_FIELDS if k in pr}
        cols = ["person_id", "spouse_id"] + list(fields)
        vals = [ids[pr["person"]], ids[pr["spouse"]] if pr.get("spouse") else None] + list(fields.values())
        cur = con.execute(f"INSERT INTO principal({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        pid = cur.lastrowid
        for role, names in pr.get("roles", {}).items():
            for pos, name in enumerate(names):
                con.execute("INSERT INTO role(principal_id,role,person_id,position) VALUES (?,?,?,?)",
                            (pid, role, ids[name], pos))
        for pos, g in enumerate(pr.get("gifts", [])):
            con.execute("INSERT INTO gift(principal_id,recipient_id,item,position) VALUES (?,?,?,?)",
                        (pid, ids[g["recipient"]], g["item"], pos))
    con.commit()


def dump_data(con: sqlite3.Connection) -> dict:
    """Inverse of load_data: the whole database as a name-keyed dict."""
    people = [{k: r[k] for k in PERSON_FIELDS} for r in con.execute("SELECT * FROM person ORDER BY id")]
    name_of = {r["id"]: r["full_name"] for r in con.execute("SELECT id, full_name FROM person")}
    principals = []
    for p in con.execute("SELECT * FROM principal ORDER BY id"):
        roles: dict[str, list[str]] = {}
        for r in con.execute("SELECT role, person_id FROM role WHERE principal_id=? ORDER BY role, position", (p["id"],)):
            roles.setdefault(r["role"], []).append(name_of[r["person_id"]])
        gifts = [{"recipient": name_of[g["recipient_id"]], "item": g["item"]}
                 for g in con.execute("SELECT recipient_id, item FROM gift WHERE principal_id=? ORDER BY position", (p["id"],))]
        entry = {"person": name_of[p["person_id"]], "spouse": name_of.get(p["spouse_id"])}
        entry.update({k: p[k] for k in PRINCIPAL_FIELDS})
        entry["roles"] = roles
        entry["gifts"] = gifts
        principals.append(entry)
    return {"people": people, "principals": principals}


# --------------------------------------------------------------------------
# Context assembly (DB rows -> plain dict used by the templates)
# --------------------------------------------------------------------------


def person_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


def load_context(con: sqlite3.Connection, principal_id: int) -> dict:
    p = con.execute("SELECT * FROM principal WHERE id=?", (principal_id,)).fetchone()
    if p is None:
        raise SystemExit(f"no principal with id {principal_id}")
    me = person_dict(con.execute("SELECT * FROM person WHERE id=?", (p["person_id"],)).fetchone())
    spouse = None
    if p["spouse_id"]:
        spouse = person_dict(con.execute("SELECT * FROM person WHERE id=?", (p["spouse_id"],)).fetchone())
    roles: dict[str, list[dict]] = {r[0]: [] for r in ROLES}
    for r in con.execute(
        "SELECT role.role, role.id AS role_id, person.* FROM role JOIN person ON person.id=role.person_id "
        "WHERE principal_id=? ORDER BY role.role, role.position",
        (principal_id,),
    ):
        d = person_dict(r)
        roles.setdefault(r["role"], []).append(d)
    gifts = [
        person_dict(r)
        for r in con.execute(
            "SELECT gift.id AS gift_id, gift.item, person.full_name AS recipient FROM gift "
            "JOIN person ON person.id=gift.recipient_id WHERE principal_id=? ORDER BY position",
            (principal_id,),
        )
    ]
    ctx = {k: p[k] for k in p.keys()}
    ctx.update(
        me=me,
        name=me["full_name"],
        spouse=spouse,
        spouse_name=spouse["full_name"] if spouse else "",
        roles=roles,
        gifts=gifts,
        date=dt.date.today().strftime("%B %d, %Y").replace(" 0", " "),
    )
    return ctx


def list_principals(con):
    return con.execute(
        "SELECT principal.id, person.full_name FROM principal JOIN person ON person.id=principal.person_id ORDER BY principal.id"
    ).fetchall()


# --------------------------------------------------------------------------
# Document templates
#
# Each template returns a list of blocks. Block kinds:
#   ("cover", title, subtitle)          cover page (title block only)
#   ("h1", text) / ("h2", text)         section / subsection headings
#   ("p", text)                          body paragraph
#   ("indent", text)                     indented line (nominations, gifts)
#   ("li", text)                         numbered/lettered list item (text carries its own number)
#   ("field", label, value, lined)       label/value row; lined=True draws a fill-in line
#   ("sig", [labels...])                 signature lines side by side
#   ("blank", n)                         n blank ruled lines
#   ("pagebreak",)
#   ("spacer", points)
# --------------------------------------------------------------------------


def join_names(names: list[str]) -> str:
    """Trust & Will style: 'A, B and C' (no Oxford comma)."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def multiline(addr: str) -> str:
    return addr.replace("\n", "<br/>")


def will_blocks(c: dict) -> list:
    name, spouse = c["name"], c["spouse_name"]
    state = c["state_name"]
    kids = [k["full_name"] for k in c["roles"]["child"]]
    guardians = [g["full_name"] for g in c["roles"]["guardian"]]
    execs = [e["full_name"] for e in c["roles"]["executor"]]
    b: list = [("cover", "Last Will & Testament of", name)]
    b += [
        ("p", f"I, {name}, being of sound mind and memory, presently residing in the State of {state}, declare the "
              "following instrument as my Last Will and Testament (this \"Will\"). I hereby revoke all Wills and codicils "
              "previously made by me."),
        ("h1", "Family Information"),
    ]
    if spouse:
        b.append(("p", f"I am married to {spouse}. Any reference in my Will to \"my spouse\" is to {spouse}."))
    n = len(kids)
    if n == 0:
        b.append(("p", "I have no children."))
    elif n == 1:
        b.append(("p", f"I have one child. My child is {kids[0]}."))
    else:
        word = NUMBER_WORDS[n] if n < len(NUMBER_WORDS) else str(n)
        b.append(("p", f"I have {word} children. They are {join_names(kids)}."))
    b += [
        ("p", "All references in my Will to \"my children\" are references to my living children listed above, as well as to "
              "any children subsequently born to me or adopted by me in a legal proceeding valid in the jurisdiction "
              "(domestic or foreign) in which it occurred. References to \"my descendants\" are to my children and their "
              "descendants, including descendants of any deceased child."),
        ("p", "I have not entered into any contract to make this Will or any devise. Any similarity between the provisions "
              "or time of execution of this Will and the provisions or time of execution of any Will of my spouse is not "
              "to be construed as evidence of such a contract."),
    ]
    if kids and guardians:
        b += [
            ("h1", "Guardianship"),
            ("h2", "Children"),
            ("p", "If necessary to appoint a conservator or guardian for any child of mine, I nominate the following Primary "
                  "Guardian to act as conservator and guardian of the person, the estate, and the property (\"Guardian\") "
                  "of each of my children. If the Primary Guardian fails or ceases to act, I nominate the following Backup "
                  "Guardians, in the order named, to act as successor Guardian of each of my children:"),
            ("p", "For each of my children, I nominate:"),
            ("indent", f"Primary Guardian: {guardians[0]};"),
        ]
        backups = guardians[1:]
        for i, g in enumerate(backups, 1):
            tail = "; then" if i < len(backups) else ""
            b.append(("indent", f"Backup Guardian {i}: {g}{tail}"))
        b += [
            ("h2", "Bond"),
            ("p", "I direct that any Guardian shall not be required to post bond or other security and shall serve free of "
                  "court supervision, to the extent possible."),
            ("h2", "Temporary Guardianship"),
            ("p", "If an individual listed above is not immediately available to act as Guardian but is expected to become "
                  "available within a reasonable time period, I nominate the next-named individuals, in the order named, "
                  "to act as temporary Guardian until the higher-named individual is available for appointment. I nominate "
                  "the individuals listed above, in the order named, to have temporary custody and care for my children "
                  "for any period between my death and the appointment of a Guardian."),
            ("h2", "Adopted Descendants"),
            ("p", "A legally adopted person in any generation and that person's descendants, including adopted descendants, "
                  "have the same rights and will be treated in the same manner under this Will as natural children of the "
                  "adopting parent if the person is legally adopted before turning 18 years old. If an adoption was legal "
                  "in the jurisdiction it occurred in at that time, then the adoption is considered legal. A fetus in utero "
                  "that is later born alive will be considered a person in being during the period of gestation."),
        ]
    gifts = c["gifts"]
    if gifts:
        b += [
            ("h1", "Specific Gifts"),
            ("p", "I leave the following specific item(s) to the person(s) or organization(s) named below, if I own such "
                  "property at the time of my death."),
        ]
        for i, g in enumerate(gifts):
            end = "." if i == len(gifts) - 1 else ";"
            b.append(("indent", f"To {g['recipient']}, I give {g['item']}{end}"))
        b.append(("p", "If any of the named beneficiaries do not survive me, the gift to that beneficiary shall be made to the "
                       "heirs-at-law of the predeceased beneficiary. If the predeceased beneficiary has no heirs-at-law, or "
                       "if any specific gift fails for any reason, then the gift to that predeceased beneficiary shall lapse "
                       "and become part of and distributed with my residuary estate."))
    lead = "Except for the gifts listed above, my Executor" if gifts else "My Executor"
    b += [
        ("h1", "Distribution of Estate"),
        ("p", f"{lead} shall distribute my estate according to the following:"),
        ("p", "If my spouse survives me, I leave all of my residuary estate to my spouse. If my spouse has predeceased me, "
              "I give my residuary estate in equal shares to my descendants, per stirpes."),
        ("h2", "Disinheritance"),
        ("p", "I intentionally and with full knowledge of the consequences omit and do not provide in this Will for any "
              "persons, descendants, or heirs that are not named or described in this Will, whether known or unknown to "
              "me. I have made no contract or agreement obligating me to leave any gifts to any person and I expressly "
              "disinherit anyone who claims otherwise."),
        ("h2", "Estate Details"),
        ("p", "My entire estate is everything I own at my death that is subject to this Will and that remains after paying "
              "all debts, administration expenses, and taxes."),
        ("h2", "Remote Contingent Distribution"),
        ("p", "If, at any time, there is no person or entity qualified to receive final distribution of my estate or any "
              "part of it, then the portion of my estate with respect to which the failure of qualified recipients has "
              "occurred shall be distributed one-half to those persons who would inherit it had I then died intestate "
              "owning the property, and one-half to those persons who would inherit it had my spouse then died intestate "
              "owning such property, all as determined and in the proportions provided by the laws then in effect."),
        ("h2", "Survivorship"),
        ("p", "A beneficiary must survive me for at least 120 hours to receive property under this Will. As used in this "
              "Will, to \"survive\" me means to be alive or in existence as an organization 120 hours after my death."),
        ("h1", "Executor of Estate"),
        ("p", f"I nominate {execs[0]} to act as my Executor and Personal Representative (\"Executor\"). If {execs[0]} fails "
              "or ceases to act as my Executor, I nominate the following Backup Executors, in the order named, to act as "
              "my successor Executor:"),
    ]
    backups = execs[1:]
    for i, e in enumerate(backups, 1):
        tail = "; then" if i < len(backups) else ";"
        b.append(("indent", f"Backup Executor {i}: {e}{tail}"))
    b += [
        ("p", "My Executor may perform every act reasonably necessary to administer my estate and any trust established "
              "under my Will. In addition to all powers and authority given by law or other provision of this Will, my "
              "Executor has the power and is specifically authorized to:"),
        ("li", "1. collect, hold, retain, invest, reinvest, sell, and manage any real or personal property, including "
               "interests in any form of business entity including limited partnerships and limited liability companies, "
               "and life, health, and disability insurance policies, without diversification as to kind, amount, or risk "
               "of non-productivity and without limitation by statute or rule of law;"),
        ("li", "2. partition, sell, exchange, grant, convey, deliver, assign, transfer, lease, option, mortgage, pledge, "
               "abandon, borrow, loan, encumber, insure, manage, control, divide, improve, and contract with respect to "
               "any property;"),
        ("li", "3. determine the nature and value of distributions and distribute assets of my estate in cash or in kind, "
               "or partly in each, at fair market value on the distribution date, without requiring pro rata distribution "
               "of specific assets and without requiring pro rata allocation of the tax bases of those assets;"),
        ("li", "4. hold any interest in nominee form, continue businesses, carry out agreements, participate in business, "
               "vote shares, exercise shareholder rights, and deal with itself, other fiduciaries, and business "
               "organizations in which my Executor may have an interest;"),
        ("li", "5. establish reserves, release powers, and prosecute, defend, abandon, pay, settle, or contest claims "
               "related to my estate or any property held by me or my estate;"),
        ("li", "6. employ attorneys, accountants, custodians for trust assets, and other agents or assistants as my "
               "Executor deems advisable to act with or without discretionary powers, and compensate them and pay their "
               "expenses from income or principal;"),
        ("li", "7. execute and deliver any instruments needed to carry out the powers of my Executor;"),
        ("li", "8. establish, create, and fund trusts to receive property distributable to any beneficiary of this Will, "
               "in the discretion of my Executor; and"),
        ("li", "9. act as my Executor or Personal Representative in any ancillary administration that may be required or "
               "desired, or to designate, compensate, remove, and transfer or pay property to any natural person or "
               "corporation to act as my Executor or Personal Representative in any such ancillary administration, and "
               "delegate any or all of the powers held by my Executor to the Executor or Personal Representative in any "
               "such ancillary administration, including the right to serve without bond or surety."),
        ("p", "My Executor may make any payments under my Will:"),
        ("li", "1. directly to a beneficiary;"),
        ("li", "2. in any form allowed by applicable state or federal law for gifts or transfers to minors or persons "
               "under disability;"),
        ("li", "3. to a beneficiary's guardian, conservator, or caregiver for the beneficiary's benefit; or"),
        ("li", "4. by direct payment of the beneficiary's expenses."),
        ("p", "If any property is distributable to a minor, my Executor may, in the sole discretion of my Executor, pay or "
              "transfer any or all of that property to another person for the use or benefit of the minor beneficiary, "
              "including a trustee of a trust for the minor beneficiary or a custodian that my Executor selects for the "
              f"minor beneficiary under the {state} Uniform Transfers to Minors Act or a similar law of any other state, "
              "until the beneficiary reaches an age selected by my Executor, but not past the age 25 or the maximum age "
              "then allowed under the applicable Uniform Transfers to Minors Act or similar law."),
        ("p", "In addition to the above powers, my Executor may, without prior authority from any court, exercise all "
              f"powers conferred by my Will, by common law, or by the {state} law or any other jurisdiction whose law "
              "applies to my Will. Except as specifically limited by my Will these powers extend to all property held by "
              "my Executor until the actual distribution of the property."),
        ("p", "To the extent possible, my Executor shall be authorized and empowered to exercise all powers independently, "
              "without limitation, and without seeking prior judicial approval for any authorized action."),
        ("h2", "Bond"),
        ("p", "To the extent permissible, my Executor is not required to give any bond, surety, or security to any court."),
        ("h2", "Compensation"),
        ("p", "My Executor is authorized and entitled to compensation as provided under the laws of any state or other "
              "jurisdiction that apply to my Will. In addition, my Executor is entitled to reimbursement for reasonable "
              "expenses incurred. A receipt by the recipient for any distribution will fully discharge my Executor if the "
              "distribution is consistent with the proper exercise of my Executor's duties under my Will."),
        ("h1", "Digital Executor"),
        ("p", "My Executor shall also be my Digital Executor."),
        ("p", "My Digital Executor is authorized and empowered to manage, distribute, and/ or terminate my digital assets "
              "exercising the judgment and care, under the circumstances then prevailing, that persons of prudence, "
              "discretion and intelligence exercise in the management of their own affairs, not in regard to speculation "
              "but in regard to the permanent disposition of their digital assets, considering the probable safety of "
              "their digital assets."),
        ("p", "I authorize the custodian of any of my digital assets to disclose and give access to my digital assets to my "
              "Digital Executor. My Digital Executor shall have the right to administer my digital assets using informal, "
              "unsupervised, or independent probate or equivalent legislation designed to operate without unnecessary "
              "intervention by the probate court. No bond or other security of any kind will be required of any Digital "
              "Executor appointed in this Will."),
        ("p", "For the purposes of this Will, digital assets mean electronic assets that are stored on my computers, "
              "electronic devices, or on any online account. Online accounts include, but are not limited to, "
              "social-networking sites, online backup services, servers, email accounts, photo and document sharing "
              "sites, financial and business accounts, domain names, virtual property, websites, and blogs."),
        ("p", "I grant to my Digital Executor the following powers:"),
        ("li", "1. The power to manage, distribute and/ or terminate my digital assets without order of court and without "
               "notice to anyone;"),
        ("li", "2. The power to access, download, and backup digital assets, to convert file formats, to access any and "
               "all devices as necessary to manage digital assets, to clear computer caches, and to delete files;"),
        ("li", "3. The power to employ and compensate counsel and other persons deemed necessary by the Digital Executor "
               "for proper administration of my digital assets;"),
        ("li", "4. The power to delegate authority when such delegation is advantageous to the estate or to the "
               "management, distribution and/ or termination of my digital assets;"),
        ("li", "5. The power to continue to exercise the powers provided in this Will notwithstanding the termination of "
               "my estate until all the digital assets of the estate have been distributed; and"),
        ("li", "6. Any additional powers conferred upon digital executors wherever my Digital Executor may act."),
        ("p", "This authority is intended to constitute \"lawful consent\" to divulge the contents of any communication or "
              "record under the Stored Communications Act, the Computer Fraud and Abuse Act, and any other state or "
              "federal law relating to digital assets, data privacy, or computer fraud. My Digital Executor shall be "
              "considered an authorized user for purposes of applicable computer-fraud and unauthorized-computer-access "
              "laws. My grant of authority is intended to provide my Digital Executor full authority to access and manage "
              "my digital assets, digital devices of any type, and online accounts, to the maximum extent permitted under "
              "applicable state and federal law and does not limit any authority granted to my Digital Executor under "
              "such laws."),
        ("h1", "Taxes, Claims, Debts, and Expenses"),
        ("p", "I direct that my Executor pay the expenses of my last illness, of my funeral, of my just debts, and of my "
              "estate's administration from my residuary estate."),
        ("p", "My Executor shall pay all estate, inheritance and similar taxes payable with respect to property included in "
              "my estate, whether or not passing under my Will, and any interest or penalties, from my residuary estate "
              "without apportionment and with no right of reimbursement from any recipient of any estate property."),
        ("p", "My Executor may make any elections allowed by the Internal Revenue Code or the laws of any state or other "
              "jurisdiction. In making such elections regarding taxes, my Executor may make such decisions as my Executor "
              "deems appropriate considering all circumstances and my Executor shall have no liability and shall have no "
              "duty to make adjustments as a result of any such election. My Executor may also execute joint tax returns, "
              "pay taxes or interest, and deal with refunds, interest, or credits as my Executor deems necessary or "
              "advisable either in the interest of the other joint taxpayer or in the interest of my estate."),
        ("p", "If payment would decrease the federal estate tax marital deduction available to my estate or violate the "
              "provisions of Treasury Regulation Section 20.2056(b)-4(d), my Executor may not pay any administrative "
              "expenses from the net income of property qualifying for the federal estate tax marital deduction."),
        ("h1", "General Provisions"),
        ("p", f"The validity and construction of my Will will be determined by the laws of the State of {state}. I have "
              "not entered into any contract, actual or implied, to make a Will."),
        ("h2", "Severability"),
        ("p", "If any part of this instrument is determined to be void or invalid, the remaining provisions will continue "
              "in full force and effect."),
        ("h2", "No-Contest Clause"),
        ("p", "If any beneficiary of my estate, alone or with any other persons, contests in court the validity of this "
              "Will or any trust receiving property under this Will, or seeks an adjudication in any proceeding in any "
              "court that this Will or any of its dispositive provisions are void, or otherwise seek to void, nullify, or "
              "set aside any of the provisions of this Will, then the right of that person to take any property, shall be "
              "revoked and shall be determined as if that contesting beneficiary had not survived me and left no "
              "heirs-at-law that could, in any case, receive the revoked share. My Executor is authorized to defend, at "
              "the expense of my estate, any contest or other attack on this Will or any of its provisions."),
        ("h2", "Gender and Grammatical Number"),
        ("p", "Unless a different construction is clearly required by the context, the masculine, feminine, and neuter "
              "genders shall each include the others, the singular and plural numbers shall include the other, and no "
              "distinction is to be drawn from the use of a particular gender or grammatical number."),
    ]
    # Final Arrangements: present in the 2020 Trust & Will document, dropped
    # from the 2026 one. Restored here, driven by the principal's choices.
    fa = []
    if c["remains"] == "cremated":
        fa.append("I direct that my body be cremated.")
    elif c["remains"] == "buried":
        fa.append("I direct that my body be buried.")
    elif c["remains"]:
        fa.append(f"I direct that {c['remains']}")
    if c["ceremony"] == "executor":
        fa.append("I direct that my executor decides what type of ceremony be held.")
    elif c["ceremony"]:
        fa.append(f"I direct that {c['ceremony']}")
    if c["final_special_request"]:
        fa.append(c["final_special_request"])
    if fa:
        b.append(("h1", "Final Arrangements"))
        for line in fa:
            b.append(("p", line))
        b.append(("p", "Any outstanding costs associated with my final arrangements shall be paid out of my estate by my "
                       "Executor."))
    b += [
        ("pagebreak",),
        ("h1", "Your Signature, Please"),
        ("p", f"I, {name}, sign my name to this instrument and attest and declare that I sign and execute this instrument "
              "as my Last Will and Testament, I sign it willingly, I execute it as my free and voluntary act for the "
              "purposes therein expressed, and I am eighteen years of age or older, of sound mind and memory, and under "
              "no duress, restraint, constraint, or undue influence. I ask the persons who sign below to be my witnesses."),
        ("spacer", 30),
        ("sig", ["Signature", "Date"]),
        ("h1", "Witnesses"),
        ("p", f"We, the undersigned witnesses to the Will of {name}, the testator, under penalty for perjury, state that, "
              "on the date written below, the testator declared to us that this instrument was the testator's Will, the "
              "testator asked us to witness it, and the testator then signed this instrument in our sight and presence, "
              "all of us being present at the same time. We believe, to the best of our knowledge, the testator is now "
              "more than 18 years of age, of a sound and disposing mind and memory, competent in every respect to make a "
              "Will, acting freely and voluntarily and not under any restraint, duress, constraint, menace, fraud, "
              "misrepresentation, or undue influence. At the testator's request, in the testator's presence, and in the "
              "presence of one another, we subscribe our names as witnesses."),
        ("spacer", 24),
        ("sig", ["First Witness Signature", "Second Witness Signature"]),
        ("sig", ["First Witness Printed Name", "Second Witness Printed Name"]),
        ("sig", ["Date", "Date"]),
        ("sig", ["First Witness Address", "Second Witness Address"]),
        ("sig", ["First Witness City, State, Zip", "Second Witness City, State, Zip"]),
        ("pagebreak",),
        ("h1", "Notary & Self-Proving Affidavit"),
        ("p", c["notary_jurisdiction"]),
        ("p", "County of _____________________"),
        ("p", f"We, {name}, ______________________________, and ______________________________, the testator and the "
              "witnesses, respectively, whose names are signed to the attached or foregoing instrument, being first duly "
              "sworn, do declare to the undersigned authority that the testator signed and executed the instrument as the "
              "testator's will and that the testator signed willingly (or willingly directed another to sign for the "
              "testator), and that the testator executed it as the testator's free and voluntary act for the purposes "
              "expressed in that document, and that each of the witnesses, in the conscious presence and hearing of the "
              "testator, signed the will as witness and that to the best of each witness' knowledge the testator was at "
              "that time eighteen years of age or older, of sound mind, and under no constraint or undue influence."),
        ("spacer", 20),
        ("sig", ["", "Testator"]),
        ("sig", ["", "Witness"]),
        ("sig", ["", "Witness"]),
        ("p", f"Subscribed, sworn to and acknowledged before me by {name}, the testator, and subscribed and sworn to "
              "before me by _____________________________ and _____________________________, witnesses, this _____ day of "
              "____________________."),
        ("spacer", 20),
        ("sig", ["(Notary's official signature)", "(seal)"]),
        ("sig", ["(official capacity of officer)", ""]),
    ]
    return b


def agent_fields(person: dict) -> list:
    out = [("field", "Agent's name:", person["full_name"], False)]
    if person["address"]:
        out.append(("field", "Agent's address:", multiline(person["address"]), False))
    if person["phone"]:
        out.append(("field", "Agent's phone:", person["phone"], False))
    if person["email"]:
        out.append(("field", "Agent's email:", person["email"], False))
    return out


def ahcd_blocks(c: dict) -> list:
    name = c["name"]
    state = c["state_name"]
    agents = c["roles"]["health_agent"]
    ordinal = ["First", "Second", "Third", "Fourth", "Fifth"]
    b: list = [("cover", "Advance Health Care Directive for", name)]
    b += [
        ("p", f"I, {name}, make this Health Care Directive to designate a health care agent, specify the powers of my "
              "health care agent, and provide other instructions regarding my health care and the authority of my health "
              "care agent."),
        ("h1", "Personal Information"),
        ("field", "My name:", name, False),
        ("field", "My address:", multiline(c["me"]["address"]), False),
        ("h1", "Designation of Health Care Agent"),
    ]
    if agents:
        b.append(("p", "I nominate the following individual to serve as my health care agent, proxy, surrogate, "
                       "representative, and any other similar term (\"Health Care Agent\"):"))
        b += agent_fields(agents[0])
    if len(agents) > 1:
        b.append(("p", "If the primary agent, named above, is unwilling, unable, or ceases to act as my Health Care Agent "
                       "for any reason, then I nominate the following individuals, in the order named to serve as my "
                       "Health Care Agent:"))
        for i, a in enumerate(agents[1:]):
            b.append(("h2", f"{ordinal[i]} Alternate Agent:"))
            b += agent_fields(a)
    b += [
        ("h1", "Effectiveness"),
        ("p", "This Health Care Directive shall become effective at any time that I am unable, in the opinion of my Health "
              "Care Agent and my attending physician, to make or communicate a choice about a particular health care "
              "decision. This Health Care Directive becomes effective upon the incapacity of the principal. This Health "
              "Care Directive shall be durable and shall remain in effect during any period of my incapacity or "
              "disability."),
        ("p", "If I am unable to make or communicate a choice about a particular health care decision, my Health Care "
              "Agent shall have any and all powers and authority to carry out and effectuate my decision."),
        ("h1", "Additional Provisions"),
        ("p", "I authorize and instruct any health care provider to rely on my Health Care Agent. No health care provider, "
              "other individual, or other institution who, in good faith, reasonably relies on any representations by my "
              "Health Care Agent will be liable to me, my estate, my heirs, or my assigns for recognizing the actual or "
              "apparent authority of my Health Care Agent."),
        ("p", "I revoke and rescind any prior health care directive, power of attorney for health care, or other "
              "designation of agent to make health care decisions for me."),
        ("p", f"I complete and execute this form in the State of {state} on the date indicated below. I intend this "
              "Health Care Directive to be universal and valid in any jurisdiction in which it is presented."),
        ("p", "I authorize my Health Care Agent to make one or more copies of this Health Care Directive. I intend copies "
              "of this document to have the same effect as the original. My Health Care Agent is authorized to provide "
              "copies to any health care provider."),
        ("p", "My Health Care Agent is not entitled to receive compensation for services performed under or in connection "
              "with this Health Care Directive. My Health Care Agent is entitled to reimbursement for reasonable expenses "
              "incurred in connection with or as a result of carrying out any provision of this Health Care Directive or "
              "in exercising any authority granted to my Health Care Agent by this document."),
        ("h1", "Instructions for Health Care"),
        ("p", "I intend this document to be a health care directive to my Health Care Agent, my doctors, and my other "
              "Health Care Providers. If the provisions are not enforceable as a health care directive, I intend these "
              "provisions be construed and given effect as a written expression of my intentions, desires, and "
              "preferences."),
        ("h2", "General Provisions for Health Care"),
        ("p", "I desire to remain in my home as long as possible. My Health Care Agent is authorized to take any actions "
              "necessary for me to remain in my home for as long as it is reasonable for me to do so. My Health Care Agent "
              "shall ensure that funds are available to pay for any in-home care provided, but my desire is to remain in "
              "my home regardless of the costs or expenses."),
        ("p", "If it is necessary to receive Care and Treatment outside of my home, my Health Care Agent is authorized to "
              "arrange for my care at any medical facility, hospital, hospice care, nursing home, or other similar "
              "facility. My Health Care Agent shall ensure that all of my essential needs are provided for and that I "
              "maintain a comfortable standard of living and hygiene. My Health Care Agent is authorized to facilitate "
              "the reasonable payment for any such services."),
        ("p", "My Health Care Agent is authorized to facilitate any activities and the involvement of any individuals in "
              "accordance with my established beliefs and customary activities known to my Health Care Agent. My Health "
              "Care Agent may facilitate the presence of any clergy or other individuals to support my beliefs and may "
              "facilitate any associated activities, materials, or services."),
        ("h2", "End of Life Decisions"),
    ]
    if c["care_preference"] == "improve_only":
        b += [
            ("p", "I do not wish to receive life-sustaining Care and Treatment that will only delay the timing of my death "
                  "without improving my condition."),
            ("p", "I do not authorize the administration of life-sustaining Care and Treatment that will only prolong my "
                  "life or delay the timing of my death without improving my condition."),
            ("p", "I authorize and desire to receive nutrition and hydration by natural means, but I do not authorize the "
                  "administration of artificial nutrition and hydration."),
        ]
    else:
        # Wording for the "prolong life" choice is not from a Trust & Will
        # document; it is a plain statement of the opposite preference.
        b += [
            ("p", "I wish to receive life-sustaining Care and Treatment to prolong my life for as long as possible, "
                  "regardless of my condition or the likelihood of recovery."),
            ("p", "I authorize the administration of artificial nutrition and hydration."),
        ]
    b += [
        ("p", "I authorize and desire Care and Treatment that will reduce or relieve my pain or discomfort, even if that "
              "Care and Treatment could or would result in physical damage, dependency, or hasten (but not intentionally "
              "cause) my death."),
        ("h2", "Definitions"),
        ("p", "\"Care and Treatment\" refers to any type of treatment or care related to my health care, including, but "
              "not limited to, any medical treatment, medical care, emergency care, surgical procedures, tests, "
              "examinations, or medications. Care and Treatment also refers to any type of treatment or care related to "
              "psychological or psychiatric care, dental care, or therapeutic care."),
        ("p", "\"Health Care Provider\" refers to any individual, organization, institution, or entity providing or "
              "supporting any Care and Treatment. Health Care Providers include, but are not limited to, medical doctors "
              "and physicians of any type; mental health providers including psychologists and psychiatrists; therapists; "
              "dentists; nurses; hospitals, clinics, and emergency care facilities; pharmacists and pharmacies; "
              "laboratories; emergency care providers, first responders, and ambulance services; nursing facilities and "
              "residential care facilities; medical insurance companies, or any other medical provider."),
        ("h1", "Post-Death Authority of Health Care Agent"),
        ("h2", "Autopsy"),
        ("p", "On my death, my Health Care Agent is authorized to authorize my autopsy."),
        ("h2", "Organ and Tissue Donation"),
    ]
    if c["organ_donation"]:
        b.append(("p", "My Health Care Agent is authorized to make an anatomical gift of my body or any of my organs, "
                       "tissues, or other parts of my body under the Uniform Anatomical Gift Act or other relevant law, "
                       "for transplant, therapy, research, education, or other purpose."))
    else:
        b.append(("p", "My Health Care Agent is not authorized to make an anatomical gift of my body or any of my organs, "
                       "tissues, or other parts of my body."))
    b += [
        ("h2", "Final Arrangements"),
        # Deviation from Trust & Will: their text cites a separate "Final
        # Arrangement Wishes" document that was never produced. This points at
        # the will's Final Arrangements section instead. See NOTES.md.
        ("p", "I have provided instructions for the disposition of my body and remains in the Final Arrangements "
              "section of my Last Will and Testament. My Health Care Agent is authorized to comply with any instructions "
              "set forth there. In carrying out those instructions, my Health Care Agent is authorized to take any "
              "actions that my Health Care Agent deems to be necessary and appropriate for my funeral or memorial and "
              "the disposition of my remains in the manner I have directed."),
        ("p", "To the extent that no such instructions can be found or to the extent that my Health Care Agent is unable "
              "to carry out those instructions for any reason, then my Health Care Agent is hereby authorized to dispose "
              "of my body and remains according to his or her absolute discretion."),
        ("p", "My Health Care Agent is empowered to authorize or incur reasonable expenses in carrying out my final "
              "arrangements. Any such expenses shall be paid out of any trust for which I am a grantor and that "
              "authorizes such payment. If no such trust exists, then any such expenses shall be paid by the executor or "
              "personal representative of my estate. My Health Care Agent is entitled to seek reimbursement for any "
              "reasonable costs advanced by my Health Care Agent."),
        ("h1", "General Provisions"),
        ("h2", "Authority of Health Care Agent"),
        ("p", "My Health Care Agent is authorized to commence, seek, continue, or deal with any judicial proceeding to "
              "determine the validity or interpretation of this document. My Health Care Agent is authorized to seek "
              "judicial remedies against any third party who is obligated to comply with this document or my Health Care "
              "Agent's instructions, but who fails to do so."),
        ("h2", "Limitations on Authority of Health Care Agent"),
        ("p", "My Health Care Agent is not authorized to consent to any of the following on my behalf: (1) commitment or "
              "placement in a mental health treatment facility; (2) electro-Convulsive therapy or shock therapy; (3) "
              "psychosurgery; (4) sterilization; or (5) abortion."),
        ("h2", "Release of Medical Information"),
        ("p", "I designate my Health Care Agent as a Personal Representative and authorize my Health Care Providers to "
              "disclose and release any Medical Information upon request of my Health Care Agent. This constitutes a full "
              "authorization to disclose any Medical Information or Individually Identifiable Health Information to my "
              "Health Care Agent, despite the protections of the Health Insurance Portability and Accountability Act of "
              "1996 and relevant state law (\"HIPAA\")."),
        ("p", "As used in this section, the terms \"Health Care Provider,\" and \"Individually Identifiable Health "
              "Information\" refer to the terms as defined by HIPAA and relevant state law. \"Medical Information\" refers "
              "to any information related in any way to my health care or Care and Treatment, including Individually "
              "Identifiable Health Information and Protected Medical Information as defined under HIPAA, and under "
              "relevant state law."),
        ("h2", "Compensation and Reimbursement of Health Care Agent"),
        ("p", "My Health Care Agent is not entitled to receive reasonable compensation for services provided pursuant to "
              "this document."),
        ("p", "My Health Care Agent is entitled to reimbursement for all reasonable costs and expenses actually incurred "
              "and paid by my Health Care Agent on my behalf pursuant to this document."),
        ("h2", "Retention of my Rights"),
        ("p", "I retain the right to make my own medical and health care decisions so long as I am able to give informed "
              "consent. I reserve the right to refuse any treatment or medical procedures, and no treatment or medical "
              "procedures may be given to me over my objection or refusal."),
        ("h2", "Revocation of Document or Termination of Health Care Agent"),
        ("p", "I reserve the right to revoke this document, terminate the authority of my Health Care Agent, or remove my "
              "Health Care Agent, with or without replacing my Health Care Agent after removal."),
        ("p", "Any such revocation, termination, or removal may be effectuated in any of the following ways:"),
        ("li", "1. By executing a written document confirming such action;"),
        ("li", "2. By destroying all copies of this document that relate to the designation of my Health Care Agent;"),
        ("li", "3. By crossing out, striking, otherwise negating the text of this document that relate to the designation "
               "of my Health Care Agent and signing such marks;"),
        ("li", "4. By conspicuously writing \"Revoked,\" \"Terminated,\" or other similar words or phrases over the text of "
               "this document that relate to the designation of my Health Care Agent and signing such marks; or"),
        ("li", "5. Any other manner permitted by law."),
        ("p", "Any such revocation, termination, or removal may be total and remove all powers and authorities granted by "
              "this document or may be partial and remove some or all powers and authorities granted to some or all "
              "Health Care Agents by this document."),
        ("h2", "Resignation of Health Care Agent"),
        ("p", "My Health Care Agent may resign by providing a written notice of resignation to me or, if I am "
              "incapacitated, to any agent serving under my durable power of attorney. If there is no such agent, or if "
              "the resigning Health Care Agent is also serving as such agent, the notice may be provided to any person "
              "that has care and custody over me."),
        ("p", "My Health Care Agent is deemed to have resigned upon 1) death, 2) adjudication of incapacity, or 3) "
              "diagnosis by two or more licensed physicians that my Health Care Agent is unable to manage his or her own "
              "personal or financial affairs."),
        ("h2", "Release of Health Care Agent"),
        ("p", "My Health Care Agent and the heirs, successors, assigns, and estate of my Health Care Agent are released "
              "and discharged by me, my heirs, successors, assigns, and estate, from any and all liability, claims, or "
              "demands related to or arising from the acts or omissions of my Health Care Agent in carrying out the "
              "duties and powers under any provision of this document, other than the willful misconduct or gross "
              "negligence of my Health Care Agent."),
        ("h2", "Copies and Effect of Copies"),
        ("p", "My Health Care Agent is authorized to make one or more copies of this document and provide such copies to "
              "Health Care Providers or other recipients, as deemed necessary by my Health Care Agent. My Health Care "
              "Agent is authorized to have a copy of this document placed in my medical records. A copy of this document "
              "has the same effect as the original."),
        ("h2", "Severability"),
        ("p", "If any part of this instrument is determined to be void or invalid, the remaining provisions will continue "
              "in full force and effect."),
        ("h1", "Powers of Health Care Agent"),
        ("p", "I give my Health Care Agent broad authority to make decisions regarding my health care wishes."),
        ("p", "My Health Care Agent has full authority to make decisions for me about my health care. To the extent my "
              "Health Care Agent knows my goals, wishes, and desires based on any oral or written communications or any "
              "other written guidance, my Health Care Agent shall make decisions in accordance with my goals, wishes, and "
              "desires. In all other instances and in any instance in which it is unclear which decision I would make for "
              "myself, my Health Care Agent shall make decisions based on what my Health Care Agent believes to be in my "
              "best interests."),
        ("p", "My Health Care Agent shall have broad authority to make decisions for me; to interpret my goals, wishes, "
              "and desires; and to determine what is in my best interests. The authority of my Health Care Agent shall "
              "include the following:"),
        ("li", "1. To agree to, refuse, or withdraw consent to any type of medical care, treatment, surgical procedure, "
               "tests, medications, or other activity related to my health care."),
        ("li", "2. To have access to medical records, health care information, protected medical information and "
               "individually identifiable health information as defined under the Health Insurance Portability and "
               "Accountability Act of 1996 and relevant state law, and any other information relevant to my Health Care "
               "Agent in carrying out the authority of my Health Care Agent, to the same extent that I am or would be "
               "entitled to, including the right to disclose any such records or information to others."),
        ("li", "3. To authorize my admission to or discharge from any hospital, nursing home, residential care, "
               "assisted-living, or other similar facility or service, even if such admission or discharge is against "
               "medical advice."),
        ("li", "4. To contract for any health care related services or facilities for me and to apply for any public or "
               "private health care benefits. My Health Care Agent shall not be personally liable or financially "
               "responsible for any such contracts."),
        ("li", "5. To hire and fire any medical, social service, or other support personnel who are responsible for or "
               "contribute to my care."),
        ("li", "6. To authorize my participation in medical research related to my medical condition, including my "
               "participation in or with any experimental or trial treatments, procedures, or medications."),
        ("li", "7. To agree to, refuse, or withdraw consent to using any medication, treatment, or procedure intended to "
               "relieve pain or discomfort, even if that use could or would result in physical damage, dependency, or "
               "hasten (but not intentionally cause) my death."),
        ("li", "8. To take any other action necessary to do what I authorize or direct in this document or in other "
               "written instructions provided to my Health Care Agent, including signing any waivers or other documents, "
               "pursuing any dispute resolution process, or taking any action in my name."),
        ("li", "_______________ By initialing here, I expressly confirm that the first enumerated power above includes "
               "making decisions about using mechanical or other procedures that may affect any bodily functions, "
               "including, but not limited to, artificial respiration, artificially-supplied nutrition and hydration, "
               "cardiopulmonary resuscitation, life support, or any type of medical support or procedure, even if the "
               "decision could or would result in my death, hasten my death, or otherwise alter the timing of my death."),
        ("li", "If I have crossed out, struck, or otherwise negated any portion of or the entirety of any power "
               "enumerated above, then such crossed out, struck, or negated text shall have no effect and shall convey no "
               "power to my Health Care Agent."),
        ("h2", "Special Instructions or Limitations"),
        ("li", "Notwithstanding any other provision of this document, my Health Care Agent shall abide by the following "
               "instructions. To the extent that any of the following provisions limit the authority of my Health Care "
               "Agent described above, the provisions here shall control and supersede any provisions listed above."),
    ]
    if c["hc_special_instructions"]:
        for para in c["hc_special_instructions"].split("\n\n"):
            b.append(("li", para.strip()))
    b += [
        ("pagebreak",),
        ("h1", "Signature"),
        ("p", "I understand the contents of this document and the effect of granting these powers to my Health Care "
              "Agent. I sign my name to this document and declare that it expresses my intent and desires. I sign "
              "willingly and as a free and voluntary act. I ask the persons who sign below to be my witnesses."),
        ("spacer", 24),
        ("sig", [name, "Date of birth: ____________________"]),
        ("spacer", 12),
        ("sig", ["Signature", "Date"]),
        ("pagebreak",),
        ("h1", "Witnesses"),
        ("p", f"We, the undersigned, state that, on the date written below, the Principal, {name}, signed this document "
              "in our presence. We know the Principal or have reviewed adequate proof of the identity of the Principal. "
              "We both witnessed the Principal sign or acknowledge this Health Care Directive in front of us. We believe "
              "that the Principal is of sound mind; under no duress, fraud, or undue influence; and signed as a free and "
              "voluntary act."),
        ("p", "Each of us is at least 18 years of age, of sound mind, and capable of being a witness. We are not any of "
              "the following:"),
        ("li", "Nominated in this document as Health Care Agent or an alternate;"),
        ("li", "Related to the Principal by blood, marriage, domestic partnership, or adoption or the spouse of any such "
               "person;"),
        ("li", "A health care provider to the Principal, including the owner or operator of any health, long-term care, "
               "or other residential or care facility serving the Principal;"),
        ("li", "An employee of any health care provider to the Principal;"),
        ("li", "Financially responsible for the health care of the Principal;"),
        ("li", "An employee of the life or health insurance provider of the Principal;"),
        ("li", "A creditor of the Principal or entitled to any assets of the Principal under a Will or codicil, trust, "
               "insurance policy, or by operation of intestate succession laws;"),
        ("li", "Entitled to benefit financially in any way after the death of the Principal."),
        ("p", "We subscribe our names as witnesses."),
        ("spacer", 24),
        ("sig", ["First Witness Signature", "Second Witness Signature"]),
        ("sig", ["First Witness Printed Name", "Second Witness Printed Name"]),
        ("sig", ["Date", "Date"]),
        ("sig", ["First Witness Address", "Second Witness Address"]),
        ("sig", ["First Witness City, State, Zip", "Second Witness City, State, Zip"]),
        ("pagebreak",),
        ("h1", "Notary (Optional)"),
        ("p", c["notary_jurisdiction"]),
        ("p", "County of ___________________________________"),
        ("p", "On this ______ day of ____________________, 20____, before me, _________________________________, the "
              "undersigned notary, personally appeared _______________________________, who proved to me on the basis of "
              "satisfactory evidence which were ___________________________________________________, to be the person "
              "whose name is subscribed to the foregoing instrument and acknowledged he or she signed the foregoing "
              "instrument."),
        ("p", "In witness whereof I hereunto set my hand."),
        ("spacer", 24),
        ("sig", ["Signature of Notary Public", "(Seal)"]),
        ("sig", ["Printed Name of Notary Public", ""]),
        ("p", "My Commission expires _________________________"),
    ]
    return b


def hipaa_blocks(c: dict) -> list:
    name = c["name"]
    b: list = [("cover", "Authorization to Release Medical Information for", name)]
    b += [
        ("p", f"I, {name}, make this Authorization to Release Medical Information (\"Authorization\") to designate the "
              "individuals authorized to receive my Medical Information and to authorize my Health Care Providers to "
              "release my Medical Information to those designated individuals."),
        ("h1", "Designation of Personal Representative To Receive Medical Information"),
        ("p", "I designate the following individuals as my Personal Representatives and I authorize my Health Care "
              "Providers to disclose and release my Medical Information to any or all of my Personal Representatives:"),
    ]
    for r in c["roles"]["hipaa_recipient"]:
        b.append(("indent", f"{r['full_name']};"))
    b += [
        ("indent", "The trustee or successor trustee of any trust for which I am a trustee or trustor;"),
        ("indent", "My personal representative, executor, administrator, or any individual serving the same or similar "
                   "capacity in connection with my estate, including any successors; and"),
        ("indent", "Any agent or successor agent named under my health care directive or other medical or health care "
                   "power of attorney."),
        ("p", "It is my intention to provide the Personal Representatives named above broad rights to access and receive "
              "my Medical Information. Despite the provisions of HIPAA, I desire my Personal Representatives have access "
              "to my Medical Information, at the request of my Personal Representative. This Authorization constitutes a "
              "full authorization to disclose any Individually Identifiable Health Information to the Personal "
              "Representatives named in this Authorization."),
        ("p", "I intend this Authorization to be broad and any questions or ambiguities regarding the provisions of this "
              "Authorization shall be resolved in favor of allowing the disclosure and release of my Medical Information "
              "to my Personal Representatives."),
        ("h1", "Definitions"),
        ("p", "The following definitions apply to this document:"),
        ("indent", "Health Care Provider refers to the term as defined by HIPAA and includes any person or entity that is "
                   "subject to restrictions or limitations regarding confidentiality, privacy, and the release or "
                   "disclosure of Medical Information. Health Care Provider includes medical doctors and physicians of any "
                   "type; mental health providers including psychologists and psychiatrists; therapists; dentists; "
                   "nurses; hospitals, clinics, and emergency care facilities; pharmacists and pharmacies; laboratories; "
                   "emergency care providers, first responders, and ambulance services; nursing facilities and "
                   "residential care facilities; medical insurance companies, or any other medical provider. Health Care "
                   "Provider also includes any \"Covered Entity\" as used in HIPAA, any health care information "
                   "clearinghouse, and any employees, officers, contractors, agents, or affiliates of any Health Care "
                   "Provider."),
        ("indent", "HIPAA refers to the Health Insurance Portability and Accountability of 1996 and relevant state law."),
        ("indent", "Individually Identifiable Health Information refers to the term as defined by HIPAA and includes any "
                   "\"Protected Medical Information\" as used in HIPAA; medical records of any past, present, or future "
                   "medical or mental health condition; records, reports, or information regarding my medical history, "
                   "diagnosis, prognosis, treatment, procedures, billing, and identification of my Health Care Providers; "
                   "and any other information related in any way to my health care."),
        ("indent", "Medical Information refers to any information related in any way to my health care, including "
                   "Individually Identifiable Health Information and Protected Medical Information as defined in this "
                   "Authorization, under HIPAA, and under relevant state law."),
        ("indent", "Personal Representative refers to the term as defined by HIPAA and includes the individuals designated "
                   "above and any personal representatives or authorized representative as used in relevant state law."),
        ("h1", "Additional Provisions"),
        ("p", "This Authorization is effective immediately upon my execution of this Authorization. This Authorization is "
              "durable and shall remain effective regardless of subsequent disability or incapacity. This Authorization "
              "shall terminate upon my written revocation received by my Health Care Provider or two years after my "
              "death, whichever occurs first."),
        ("p", "This Authorization is in addition to and does not revoke or supersede any other authorizations I have "
              "granted in the past or may grant in the future. This Authorization does not replace any Advance Health "
              "Care Directive or medical power of attorney and any actual or perceived conflict with such documents does "
              "not affect the validity or scope of this Authorization. I reserve the right to revoke this Authorization "
              "in writing. Unless my Health Care Providers know of my revocation of this Authorization, my Health Care "
              "Providers may continue to rely on the validity and effectiveness of this Authorization."),
        ("p", "I authorize and direct my Health Care Providers to provide information at the request of my Personal "
              "Representatives; answer questions asked by my Personal Representatives; and discuss my condition, "
              "treatment, test results, prognosis, and any other details of my health care, at the request of my "
              "Personal Representative."),
        ("p", "I authorize my Health Care Providers to provide my Personal Representatives with a written statement, at "
              "the request of my Personal Representative, regarding:"),
        ("li", "1. my competency or incompetency to manage my financial and personal affairs, or"),
        ("li", "2. my diagnosis of being in an irreversible coma, in a persistent vegetative state with no reasonable "
               "possibility of returning to a cognitive life, or having incurable, irreversible, or terminal condition "
               "that is reasonably likely to result in my death within one year."),
        ("p", "Any person authorized to receive my Medical Information may bring a legal action against any Health Care "
              "Provider that fails to accept this Authorization or refuses to provide my Medical Information for any "
              "purpose authorized by this Authorization."),
        ("p", "I acknowledge that my Medical Information may not be protected by HIPAA after disclosure to my Personal "
              "Representatives and that my Medical Information may be re-disclosed by my Personal Representatives. I "
              "indemnify my Health Care Providers for any consequences of complying with this Authorization, including "
              "any consequences arising from the use of my Medical Information following an authorized disclosure and "
              "release to my Personal Representatives. No Health Care Provider may require any further indemnification "
              "by my Personal Representatives as a condition for the disclosure and release of my Medical Information. I "
              "release any Health Care Provider that relies on and acts in accordance with this Authorization in "
              "releasing and disclosing my Medical Information to my Personal Representatives."),
        ("p", "I acknowledge that Health Care Providers may not condition the provision of treatment, payment, enrollment "
              "in a health plan, or eligibility benefits upon the provision of this Authorization, except as provided "
              "under HIPAA."),
        ("p", "Each of my Personal Representatives has equal authority to request and receive my Medical Information. "
              "Each of my Personal Representatives may act independently and without the consent of any other of my "
              "Personal Representatives."),
        ("p", "My Personal Representatives are authorized and empowered to make one or more copies of this Authorization "
              "and provide such copies to my Health Care Providers. A copy of this Authorization has the same effect as "
              "the original."),
        ("pagebreak",),
        ("h1", "Signature"),
        ("p", f"I, {name}, sign my name to this instrument and declare that I execute it as my free and voluntary act for "
              "the purposes expressed therein."),
        ("spacer", 24),
        ("sig", [name, "Date of birth: ____________________"]),
        ("spacer", 12),
        ("sig", ["Signature", "Date"]),
    ]
    return b


def poa_blocks(c: dict) -> list:
    name = c["name"]
    agents = c["roles"]["poa_agent"][:2]
    backups = c["roles"]["poa_backup"][:3]

    def agent_rows(label_name, label_addr, people, slots):
        rows = []
        for i in range(slots):
            if i > 0:
                rows.append(("p", "– AND – (optional)"))
            p = people[i] if i < len(people) else None
            rows.append(("field", label_name, p["full_name"] if p else "", True))
            rows.append(("field", label_addr, p["address"].replace("\n", ", ") if p else "", True))
        return rows

    b: list = [("cover", "Durable Power of Attorney for", name, c["poa_statute"])]
    b += [
        ("h1", "Designation of Agent"),
        ("p", f"I, {name}, of {c['residence_line']} appoint:"),
    ]
    b += agent_rows("Agent's name:", "Agent's address:", agents, 2)
    b += [
        ("p", "as my agent (attorney-in-fact) to act for me in any lawful way, as provided in this Power of Attorney."),
        ("h1", "Designation of Successor Agent(s) – Optional"),
        ("p", "When multiple agents are serving under this Power of Attorney, if any agent ceases to serve as my agent due "
              "to death, resignation, incapacity, or any other reason, then the remaining agent or agents shall continue "
              "to serve in that capacity. If all of the agents cease to serve as my agent due to death, resignation, "
              "incapacity, or any other reason, then I name the following individuals to act as my successor agent, "
              "individually and in the order named:"),
    ]
    b += agent_rows("Backup Agent's name", "Backup Agent's address", backups, 3)
    b += [
        ("h1", "Multiple Agents"),
        ("p", "When multiple agents are serving jointly under this Power of Attorney, all of them must sign or act "
              "together."),
        ("h1", "Durability"),
        ("p", "This power of attorney is durable and shall not be affected by my subsequent disability or incapacity, or "
              "by the lapse of time. I intend the powers granted to my agent in this Power of Attorney to be exercisable "
              "by my agent even if I am adjudicated to be totally or partially incapacitated by a court."),
        ("h1", "Powers of Agent"),
        ("p", "I grant my agent general authority to act for me with respect to any matters and any affairs. In addition, "
              "my agent is authorized to act for me and in my name and may exercise any of the powers described below:"),
        ("h2", "A. Real Property Transactions"),
        ("li", "My agent may exercise any power I have with respect to any real property or interest in real property "
               "that I own. My agent may: (1) demand, buy, lease, receive, accept as a gift or as security for an "
               "extension of credit, or otherwise acquire or reject an interest in real property or a right incident to "
               "real property; (2) sell; exchange; convey with or without covenants, representations, or warranties; "
               "quitclaim; release; surrender; retain title for security; encumber; partition; consent to partitioning; "
               "subject to an easement or covenant; subdivide; apply for zoning or other governmental permits; plat or "
               "consent to platting; develop; grant an option concerning; lease; sublease; contribute to an entity in "
               "exchange for an interest in that entity; or otherwise grant or dispose of an interest in real property "
               "or a right incident to real property; (3) pledge or mortgage an interest in real property or right "
               "incident to real property as security to borrow money or pay, renew, or extend the time of payment of a "
               "debt of mine or a debt guaranteed by me; (4) release, assign, satisfy, or enforce by litigation or "
               "otherwise a mortgage, deed of trust, conditional sale contract, encumbrance, lien, or other claim to real "
               "property which exists or is asserted; (5) manage or conserve an interest in real property or a right "
               "incident to real property owned or claimed to be owned by me, including insuring against liability or "
               "casualty or other loss; obtaining or regaining possession of or protecting the interest or right by "
               "litigation or otherwise; paying, assessing, compromising, or contesting taxes or assessments or applying "
               "for and receiving refunds in connection with them; and purchasing supplies, hiring assistance or labor, "
               "and making repairs or alterations to the real property; (6) use, develop, alter, replace, remove, erect, "
               "or install structures or other improvements upon real property in or incident to which I have, or claim "
               "to have, an interest or right; (7) participate in a reorganization with respect to real property or an "
               "entity that owns an interest in or right incident to real property and receive, and hold, and act with "
               "respect to stocks and bonds or other property received in a plan of reorganization, including selling "
               "or otherwise disposing of them; exercising or selling an option, right of conversion, or similar right "
               "with respect to them; and exercising any voting rights in person or by proxy; (8) change the form of "
               "title of an interest in or right incident to real property; and (9) dedicate to public use, with or "
               "without consideration, easements or other real property in which I have, or claim to have, an interest."),
        ("li", "These powers and authorities also apply to any homestead property I own. If I am married, my agent may "
               "not mortgage, convey, transfer, or encumber my homestead property without the written consent of my "
               "spouse or the legal representative of my spouse."),
        ("h2", "B. Tangible Personal Property Transactions"),
        ("li", "My agent may exercise any power I have with respect to any tangible personal property or interest in "
               "tangible personal property that I own. My agent may: With regard to tangible personal property "
               "transactions, my agent may exercise all of the following powers: (1) demand, buy, receive, accept as a "
               "gift or as security for an extension of credit, or otherwise acquire or reject ownership or possession "
               "of tangible personal property or an interest in tangible personal property; (2) sell; exchange; convey "
               "with or without covenants, representations, or warranties; quitclaim; release; surrender; create a "
               "security interest in; grant options concerning; lease; sublease; or, otherwise dispose of tangible "
               "personal property or an interest in tangible personal property; (3) grant a security interest in "
               "tangible personal property or an interest in tangible personal property as security to borrow money or "
               "pay, renew, or extend the time of payment of my debt or a debt guaranteed by me; (4) release, assign, "
               "satisfy, or enforce by litigation or otherwise, a security interest, lien, or other claim on my behalf, "
               "with respect to tangible personal property or an interest in tangible personal property; (5) manage or "
               "conserve tangible personal property or an interest in tangible personal property on my behalf, including "
               "insuring against liability or casualty or other loss; obtaining or regaining possession of or protecting "
               "the property or interest, by litigation or otherwise; paying, assessing, compromising, or contesting "
               "taxes or assessments or applying for and receiving refunds in connection with taxes or assessments; "
               "moving the property from place to place; storing the property for hire or on a gratuitous bailment; and "
               "using and making repairs, alterations, or improvements to the property; and (6) change the form of title "
               "of an interest in tangible personal property."),
        ("h2", "C. Investment Transactions"),
        ("li", "My agent has broad and general authority and may (1) buy, sell, and exchange stocks and bonds or any other "
               "investment instruments; (2) establish, continue, modify, or terminate an account with respect to stocks "
               "and bonds or any other investment instruments; (3) pledge stocks and bonds or any other investment "
               "instruments as security to borrow, pay, renew, or extend the time of payment of a debt of mine; (4) "
               "receive certificates and other evidence of ownership with respect to stocks and bonds or any other "
               "investment instruments; and (5) exercise voting rights with respect to stocks and bonds or any other "
               "investment instruments in person or by proxy, enter into voting trusts, and consent to limitations on the "
               "right to vote; and (6) buy, sell, trade, or deal with any futures and options of any type related to "
               "stocks and bonds or any other investment instruments."),
        ("li", "As used in this Power of Attorney, \"investment instruments\" refers to any stocks, bonds, mutual funds, "
               "or other securities; any types of financial instruments; any types of futures or option contracts, "
               "mutual funds, money market funds, hedge funds, private equity funds, venture capital funds, or other "
               "manner of investment, whether held directly, indirectly, or in any other way, including in any entity "
               "or trust."),
        ("h2", "D. Banking and Other Financial Institution Transactions"),
        ("li", "My agent has broad and general authority and may (1) continue, modify, and terminate an account or other "
               "banking arrangement made by me or on my behalf; (2) establish, modify, and terminate an account or other "
               "banking arrangement with a bank, trust company, savings and loan association, credit union, thrift "
               "company, brokerage firm, or other financial institution selected by my agent; (3) contract for services "
               "available from a financial institution, including renting a safe deposit box or space in a vault; (4) "
               "withdraw, by check, order, electronic funds transfer, or otherwise, money or property of mine deposited "
               "with or left in the custody of a financial institution; (5) receive statements of account, vouchers, "
               "notices, and similar documents from a financial institution and act with respect to them; (6) enter a "
               "safe deposit box or vault and withdraw or add to the contents; (7) borrow money and pledge as security "
               "any of my personal property necessary to borrow money or pay, renew, or extend the time of payment of a "
               "debt of mine or a debt guaranteed by me; (8) make, assign, draw, endorse, discount, guarantee, and "
               "negotiate promissory notes, checks, drafts, and other negotiable or nonnegotiable paper of mine or "
               "payable to me or to my order, transfer money, receive the cash or other proceeds of those transactions, "
               "and accept a draft drawn by a person and pay it when due; (9) receive for me and act upon a sight draft, "
               "warehouse receipt, or other document of title whether tangible or electronic, or other negotiable or "
               "nonnegotiable instrument; (10) apply for, receive, and use letters of credit, credit and debit cards, "
               "electronic transaction authorizations, and traveler's checks from a financial institution and give an "
               "indemnity or other agreement in connection with letters of credit; and (11) consent to an extension of "
               "the time of payment with respect to commercial paper or a financial transaction with a financial "
               "institution."),
        ("h2", "E. Business Operation Transactions"),
        ("li", "My agent may exercise any power I have with respect to business operation transactions. My agent has "
               "broad and general authority and may: (1) operate, buy, sell, enlarge, reduce, or terminate an ownership "
               "interest; (2) perform a duty or discharge a liability and exercise in person or by proxy a right, power, "
               "privilege, or option that I have, may have, or claim to have; (3) enforce the terms of an ownership "
               "agreement; (4) initiate, participate in, submit to alternative dispute resolution, settle, oppose, or "
               "propose or accept a compromise with respect to litigation to which I am a party because of an ownership "
               "interest; (5) exercise in person or by proxy, or enforce by litigation or otherwise, a right, power, "
               "privilege, or option I have or claim to have as the holder of stocks and bonds; (6) initiate, participate "
               "in, submit to alternative dispute resolution, settle, oppose, or propose or accept a compromise with "
               "respect to litigation to which I am a party concerning stocks and bonds; (7) put additional capital into "
               "an entity or business in which I have an interest; (8) join in a plan of reorganization, consolidation, "
               "conversion, domestication, or merger of the entity or business; (9) sell or liquidate all or part of an "
               "entity or business; (10) establish the value of an entity or business under a buy-out agreement to which "
               "I am a party; (11) prepare, sign, file, and deliver reports, compilations of information, returns, or "
               "other papers with respect to an entity or business and make related payments; and (12) pay, compromise, "
               "or contest taxes, assessments, fines, or penalties and perform any other act to protect me from illegal "
               "or unnecessary taxation, assessments, fines, or penalties, with respect to an entity or business, "
               "including attempts to recover, in any manner permitted by law, money paid before or after the execution "
               "of this Power of Attorney."),
        ("li", "Additionally, with respect to an entity or business owned solely by me, my agent has broad and general "
               "authority and may: (A) continue, modify, renegotiate, extend, and terminate a contract made by me or on "
               "my behalf with respect to the entity or business before execution of the Power of Attorney; (B) "
               "determine the location of its operation; the nature and extent of its business; the methods of "
               "manufacturing, selling, merchandising, financing, accounting, and advertising employed in its operation; "
               "the amount and types of insurance carried; and the mode of engaging, compensating, and dealing with its "
               "employees and accountants, attorneys, or other advisors; (C) change the name or form of organization "
               "under which the entity or business is operated and enter into an ownership agreement with other persons "
               "to take over all or part of the operation of the entity or business; and (D) demand and receive money "
               "due or claimed by me or on my behalf in the operation of the entity or business and control and disburse "
               "the money in the operation of the entity or business."),
        ("h2", "F. Insurance Transactions"),
        ("li", "My agent may exercise any power I have with respect to insurance transactions. My agent has broad and "
               "general authority and may: (1) continue, pay the premium or make a contribution on, modify, exchange, "
               "rescind, release, or terminate a contract procured by me or on my behalf which insures or provides an "
               "annuity to either me or another person, whether or not I am a beneficiary under the contract; (2) "
               "procure new, different, and additional contracts of insurance and annuities for me and my spouse, "
               "children, and other dependents, and select the amount, type of insurance or annuity, and mode of "
               "payment; (3) pay the premium or make a contribution on, modify, exchange, rescind, release, or terminate "
               "a contract of insurance or annuity procured by the agent; (4) apply for and receive a loan secured by a "
               "contract of insurance or annuity; (5) surrender and receive the cash surrender value on a contract of "
               "insurance or annuity; (6) exercise an election; (7) exercise investment powers available under a "
               "contract of insurance or annuity; (8) change the manner of paying premiums on a contract of insurance or "
               "annuity; (9) change or convert the type of insurance or annuity with respect to which I have or claim to "
               "have authority described in this section; (10) apply for and procure a benefit or assistance under a "
               "statute or regulation to guarantee or pay premiums of a contract of insurance on my life; (11) collect, "
               "sell, assign, hypothecate, borrow against, or pledge my interest in a contract of insurance or annuity; "
               "(12) select the form and timing of the payment of proceeds from a contract of insurance or annuity; and "
               "(13) pay, from proceeds or otherwise, compromise or contest, and apply for refunds in connection with, a "
               "tax or assessment levied by a taxing authority with respect to a contract of insurance or annuity or its "
               "proceeds or liability accruing by reason of the tax or assessment."),
        ("h2", "G. Estate, Trust, and Other Beneficiary Transactions"),
        ("li", "My agent may exercise any power I have with respect to estate, trust, and other beneficiary transactions. "
               "My agent has broad and general authority and may: (1) accept, receive, receipt for, sell, assign, pledge, "
               "or exchange a share in or payment from an estate, trust, or other beneficiary transaction; (2) demand or "
               "obtain money or another thing of value to which I am, may become, or claim to be, entitled by reason of "
               "an estate, trust, or other beneficiary transaction, by litigation or otherwise; (3) exercise for my "
               "benefit a presently exercisable general power of appointment I hold; (4) initiate, participate in, "
               "submit to alternative dispute resolution, settle, oppose, or propose or accept a compromise with respect "
               "to litigation to ascertain the meaning, validity, or effect of a deed, will, declaration of trust, or "
               "other instrument or transaction affecting my interest; (5) initiate, participate in, submit to "
               "alternative dispute resolution, settle, oppose, or propose or accept a compromise with respect to "
               "litigation to remove, substitute, or surcharge a fiduciary; (6) conserve, invest, disburse, or use "
               "anything received for an authorized purpose; (7) transfer my interest in real property, stocks and "
               "bonds, accounts with financial institutions or securities intermediaries, insurance, annuities, and "
               "other property to the trustee of a revocable trust created by me as settlor; (8) reject, renounce, "
               "disclaim, release, or consent to a reduction in or modification of a share in or payment from an estate, "
               "trust, or other beneficiary transaction."),
        ("li", "As used in this Power of Attorney, \"estate, trust, and other beneficiary transactions\" refers to a "
               "trust, probate estate, guardianship, conservatorship, escrow, custodianship, other fiduciary "
               "relationship, or a fund from which I am, may become, or claim to be, entitled to a share or payment."),
        ("h2", "H. Claims and Litigation"),
        ("li", "My agent may exercise any power I have with respect to claims and litigation. My agent has broad and "
               "general authority and may: (1) assert and maintain before a court or administrative agency a claim, claim "
               "for relief, cause of action, counterclaim, offset, recoupment, or defense, including an action to recover "
               "property or other thing of value, recover damages sustained by me, eliminate or modify tax liability, or "
               "seek an injunction, specific performance, or other relief; (2) bring an action to determine adverse "
               "claims or intervene or otherwise participate in litigation; (3) seek an attachment, garnishment, order "
               "of arrest, or other preliminary, provisional, or intermediate relief and use an available procedure to "
               "effect or satisfy a judgment, order, or decree; (4) make or accept a tender, offer of judgment, or "
               "admission of facts, submit a controversy on an agreed statement of facts, consent to examination, and "
               "bind me in litigation; (5) submit to alternative dispute resolution, settle, and propose or accept a "
               "compromise; (6) waive the issuance and service of process upon me, accept service of process, appear for "
               "me, designate persons upon which process directed to me may be served, execute and file or deliver "
               "stipulations on my behalf, verify pleadings, seek appellate review, procure and give surety and "
               "indemnity bonds, contract and pay for the preparation and printing of records and briefs, receive, "
               "execute, and file or deliver a consent, waiver, release, confession of judgment, satisfaction of "
               "judgment, notice, agreement, or other instrument in connection with the prosecution, settlement, or "
               "defense of a claim or litigation; (7) act for me with respect to bankruptcy or insolvency, whether "
               "voluntary or involuntary, concerning me or some other person, or with respect to a reorganization, "
               "receivership, or application for the appointment of a receiver or trustee which affects my interest in "
               "property or other thing of value; (8) pay a judgment, award, or order against me or a settlement made in "
               "connection with a claim or litigation; and (9) receive money or other thing of value paid in settlement "
               "of or as proceeds of a claim or litigation."),
        ("h2", "I. Personal and Family Maintenance"),
        ("li", "My agent may exercise any power I have with respect to personal and family maintenance. My agent has "
               "broad and general authority and may: (1) perform the acts necessary to maintain the customary standard "
               "of living of me and any individuals entitled to support; (2) make periodic payments of child support and "
               "other family maintenance required by a court or governmental agency or an agreement to which I am a "
               "party; (3) provide living quarters for any individuals entitled to support by purchase, lease, or other "
               "contract; or by paying the operating costs, including interest, amortization payments, repairs, "
               "improvements, and taxes, for premises owned by me or occupied by any individuals entitled to support; "
               "(4) provide normal domestic help, usual vacations and travel expenses, and funds for shelter, clothing, "
               "food, appropriate education, including postsecondary and vocational education, and other current living "
               "costs for any individuals entitled to support; (5) pay expenses for necessary health care and custodial "
               "care on behalf of any individuals entitled to support; (6) act as my personal representative pursuant to "
               "the Health Insurance Portability and Accountability Act, Sections 1171 through 1179 of the Social "
               "Security Act, 42 U.S.C. Section 1320d, as amended, and applicable regulations, in making decisions "
               "related to the past, present, or future payment for the provision of health care consented to by me or "
               "anyone authorized under the law of this state to consent to health care on my behalf; (7) continue any "
               "provision made by me for automobiles or other means of transportation, including registering, licensing, "
               "insuring, and replacing them, for any individuals entitled to support; (8) maintain credit and debit "
               "accounts and open new accounts for the convenience of any individuals entitled to support; and (9) "
               "continue payments incidental to my membership or affiliation in a religious institution, club, society, "
               "order, or other organization or to continue contributions to those organizations."),
        ("li", "The power and authority granted to my agent by paragraph (6) of this section relates only to my agent's "
               "authority to take action and make decisions regarding the payment for the provision of health care. This "
               "Power of Attorney conveys no authority to my agent to make any medical or health-care decisions for me."),
        ("li", "As used in this Power of Attorney, \"individuals entitled to support\" refers to me, my spouse, my "
               "children, any individuals who are legally entitled to receive support from me, and any individuals that "
               "I have customarily supported or indicated an intent to support. Individuals entitled to support may "
               "include persons now living or born in the future. In determining whether I have customarily supported or "
               "indicated an intent to support any individuals, my agent may consider evidence that exists at any time "
               "before acting, even if that evidence arises after the date of this Power of Attorney."),
        ("h2", "J. Benefits From Certain Governmental Programs or Civil or Military Service"),
        ("li", "My agent may exercise any power I have with respect to benefits from certain governmental programs or "
               "civil or military service. My agent has broad and general authority and may: (1) execute vouchers in my "
               "name for allowances and reimbursements payable by the United States or a foreign government or by a "
               "state or subdivision of a state to me, including allowances and reimbursements for transportation of "
               "individuals entitled to support and for the shipment of household effects; (2) take possession and order "
               "the removal and shipment of my property from a post, warehouse, depot, dock, or other place of storage "
               "or safekeeping, either governmental or private, and execute and deliver a release, voucher, receipt, "
               "bill of lading, shipping ticket, certificate, or other instrument for that purpose; (3) enroll in, apply "
               "for, select, reject, change, amend, or discontinue, on my behalf, a benefit or program; (4) prepare, "
               "file, and maintain a claim of mine for a benefit or assistance, financial or otherwise, to which I may "
               "be entitled under a statute or regulation; (5) initiate, participate in, submit to alternative dispute "
               "resolution, settle, oppose, or propose or accept a compromise with respect to litigation concerning any "
               "benefit or assistance I may be entitled to receive under a statute or regulation; and (6) receive the "
               "financial proceeds of a claim described in this section and conserve, invest, disburse, or use for a "
               "lawful purpose anything so received."),
        ("li", "As used in this Power of Attorney, \"benefits from certain governmental programs or civil or military "
               "service\" refers to any benefit, program, or assistance provided under a statute or regulation, including "
               "Social Security, Medicare, and Medicaid."),
        ("h2", "K. Retirement Plan Transactions"),
        ("li", "My agent may exercise any power I have with respect to retirement plan transactions. My agent has broad "
               "and general authority and may: (1) select the form and timing of payments under a retirement plan and "
               "withdraw benefits from a plan; (2) make a rollover, including a direct trustee-to-trustee rollover, of "
               "benefits from one retirement plan to another; (3) establish a retirement plan in my name; (4) make "
               "contributions to a retirement plan; (5) exercise investment powers available under a retirement plan; "
               "(6) borrow from, sell assets to, or purchase assets from a retirement plan; (7) receive, endorse, and "
               "cash payments from a retirement plan; and (8) request and receive information relating to me and my "
               "retirement plans."),
        ("li", "As used in this Power of Attorney, \"retirement plans\" refers to a plan or account created by me, an "
               "employer, or another individual to provide retirement benefits or deferred compensation of which I am a "
               "participant, beneficiary, or owner, including a plan or account under the following sections of the "
               "Internal Revenue Code (\"IRC\"): (A) an individual retirement account under IRC Section 408; (B) a Roth "
               "individual retirement account under IRC Section 408A; (C) a deemed individual retirement account under "
               "IRC Section 408(q); (D) an annuity or mutual fund custodial account under IRC Section 403(b); (E) a "
               "pension, profit-sharing, stock bonus, or other retirement plan qualified under IRC Section 401(a); (F) a "
               "plan under IRC Section 457(b); and (G) a nonqualified deferred compensation plan under IRC Section 409A."),
        ("h2", "L. Tax Matters"),
        ("li", "My agent may exercise any power I have with respect to tax matters. My agent has broad and general "
               "authority and may: (1) prepare, sign, and file federal, state, local, and foreign income, gift, payroll, "
               "property, Federal Insurance Contributions Act, and other tax returns, claims for refunds, requests for "
               "extension of time, petitions regarding tax matters, and any other tax-related documents, including "
               "receipts, offers, waivers, consents, including consents and agreements under IRC Section 2032A, closing "
               "agreements, and any Power of Attorney required by the Internal Revenue Service or any state or other "
               "taxing authority with respect to a tax year upon which the statute of limitations has not run and 25 tax "
               "years after that tax year; (2) pay taxes due, collect refunds, post bonds, receive confidential "
               "information, and contest deficiencies determined by the Internal Revenue Service or any state or other "
               "taxing authority; (3) exercise any election available to me under federal, state, local, or foreign tax "
               "law; (4) act for me in all tax matters for all periods before the Internal Revenue Service or any state "
               "or other taxing authority; and (5) represent me, and appoint an agent or agents to represent me, before "
               "the Internal Revenue Service or any state or other taxing authority by completing, signing, and "
               "submitting IRS Form 2848 or any other governmental form."),
        ("h2", "M. Digital Assets"),
        ("li", "My agent may exercise any power I have with respect to digital assets. My agent has broad and general "
               "authority and may: (1) access, use, and control my digital assets; (2) access, modify, delete, control, "
               "and transfer my digital assets; (3) deal in any way that I could with any service providers related to "
               "any digital assets or any entities that hold any digital assets; and (4) access and utilize any user "
               "names, passwords, or other login information in order to access my digital assets and exercise any "
               "powers granted to my agent in this Power of Attorney."),
        ("li", "As used in this Power of Attorney, \"digital assets\" includes any digital devices, such as computers, "
               "tablets, cell phones, smart phones, peripherals, or any similar digital devices which may exist now or in "
               "the future. \"Digital Assets\" also includes any digital communications, emails and email accounts, "
               "digital media, photos, videos, audio files, licensing, social network accounts, file sharing accounts, "
               "financial accounts, web domains, tax preparation service accounts, online shopping accounts, password "
               "management accounts, affiliate programs, and any other type of online account, digital account, or other "
               "digital items which may exist now or in the future."),
        ("h1", "General Power to Agent"),
        ("p", "I grant my agent full power and authority to act for me and in my name, in any lawful way, in all matters, "
              "and in all affairs. This authority does not include any authority to make health care decisions for me."),
        ("h1", "Additional Specific Powers"),
        ("p", "In addition to all other powers and authorities granted by this document, I additionally grant my agent "
              "full power and authority to:"),
        ("li", "a. <u>Make Gifts:</u> My agent may make gifts of any of my property, outright, in trust, or otherwise, to "
               "my spouse, descendants or charitable organizations, up to the annual aggregate value, per donee, that "
               "qualifies for the Federal gift tax exclusion. If I am married and if my spouse agrees to split gifts for "
               "Federal gift tax purposes, this limit is instead up to the annual aggregate value, per donee, that "
               "qualifies for the Federal gift tax exclusion when split with my spouse, considering any other gifts made "
               "by my spouse. For any gifts made to my agent or to anyone that my agent is legally obligated to support, "
               "the aggregate value of gifts in any calendar year shall not exceed $5,000 or 5% of the total value of "
               "the assets subject to this power of attorney, valued as of the date of the gift."),
        ("li", "b. <u>Transfer Property to Trust:</u> My agent may transfer all or any part of my assets or interests in "
               "assets to any trust of which I am a settlor and beneficiary."),
        ("li", "c. <u>Deal with Powers:</u> My agent may exercise, release, or allow any powers I have to lapse, including "
               "any power of appointment or power to amend, revoke, or withdraw from any trust. The power to amend, "
               "revoke, or withdraw from any trust that I have created may only be exercised as provided in that trust "
               "document. My agent does not have the power to exercise any trustee powers of an irrevocable trust of "
               "which my agent is a settlor and not a trustee."),
        ("li", "d. <u>Make Loans:</u> My agent may loan my property to my spouse or descendants, their personal "
               "representatives, or trustees of any trusts for their benefit. These loans may be made on the interest "
               "and security terms that my agent deems appropriate."),
        ("li", "e. <u>Deal with Retirement Accounts:</u> My agent may take any action to establish any type of retirement "
               "account. My agent may contribute to any existing or new retirement account, rollover or transfer "
               "benefits into other retirement accounts, manage any retirement accounts, and make withdrawals as may be "
               "required by law or make any other withdrawals that may be necessary for my health, education, "
               "maintenance, and support. My agent may also apply for and make elections for any retirement account in "
               "which I am a participant, receive benefits, and distribute benefits to me or for my benefit, and "
               "designate or change beneficiaries on retirement accounts. My agent does not have the power to designate "
               "himself or herself as beneficiary on any retirement account, unless my agent is my spouse."),
        ("li", "f. <u>Seek Judicial Enforcement:</u> My agent may seek court orders that mandate acts that my agent deems "
               "appropriate, if necessary to compel a third party to comply with such acts. My agent may also seek court "
               "orders enjoining acts that my agent has not authorized. My agent may also sue or bring actions against "
               "any third party who fails to comply with the authorized acts and direction of my agent and my agent may "
               "seek damages of any type."),
        ("li", "g. <u>Exercise Powers with Respect to Banking and Financial Institutions:</u> My agent may exercise any "
               "powers granted with respect to banking and financial institutions under applicable law."),
        ("li", "h. <u>Access and Manage Digital Assets:</u> My agent may act on my behalf with respect to all digital "
               "assets and digital accounts, including electronic devices, electronic accounts, online accounts, email "
               "accounts, financial accounts, and any other type of electronic, digital, or online accounts or items. My "
               "agent has full authority to access and manage my digital assets, digital devices of any type, and online "
               "accounts, to the maximum extent permitted under applicable state and federal law."),
        ("li", "i. <u>Other Acts:</u> My agent may do anything I can do through an agent for the welfare of my spouse, "
               "descendants, dependents, pets, or to preserve relationships with my spouse, descendants, other "
               "relatives, friends, and organizations."),
        ("h1", "Limitations on Agent's Authority"),
        ("p", "In addition to the limits provided within the powers listed above, my agent is subject to the following "
              "limits and restrictions:"),
        ("li", "a. <u>Fiduciary Capacity:</u> My agent shall exercise any powers granted by this document in a fiduciary "
               "capacity."),
        ("li", "b. <u>Limits on Power to Appoint Assets:</u> My agent does not have the power to give, assign, or disclaim "
               "any of my assets or interests to my agent or the estate of my agent or to creditors of my agent or the "
               "estate of my agent. My agent does not have the power to use any of my assets to discharge the legal "
               "obligations of my agent, other than any legal obligations to me or legal obligations that I also have. "
               "My agent is not limited from using my assets to discharge any legal obligation I have to my agent."),
        ("h1", "Compensation and Reimbursement"),
        ("p", "My agent is entitled to receive reasonable compensation for the services provided pursuant to this Power "
              "of Attorney. My agent is entitled to reimbursement for all reasonable costs and expenses actually incurred "
              "and paid by my agent on my behalf."),
        ("h1", "Additional Instructions"),
        ("p", "Notwithstanding any other provision of this document, my agent shall abide by the following instructions. "
              "To the extent that any of the following provisions limit the authority of my agent described above, the "
              "provisions below shall control and supersede any provisions listed above."),
    ]
    if c["poa_special_instructions"]:
        for para in c["poa_special_instructions"].split("\n\n"):
            b.append(("p", para.strip()))
    else:
        b.append(("p", "You may give special instructions on the following lines:"))
        b.append(("blank", 14))
    b += [
        ("p", "Notwithstanding any provision herein to the contrary, any authority granted to my agent shall be limited so "
              "as to prevent this instrument from causing my agent to be taxed on my income (unless my agent is my "
              "spouse) and from causing my assets to be subject to a general power of appointment by my agent, as that "
              "term is defined in Section 2041 of the Internal Revenue Code."),
        ("h1", "Effective Date"),
        ("p", "This power of attorney is effective immediately unless I have stated otherwise in the Special "
              "Instructions."),
        ("h1", "Reliance on this Power of Attorney"),
        ("p", "Any third parties may rely upon a copy of this Power of Attorney that is certified by my agent to be a true "
              "and complete copy of the original to the same extent as if the third party had received an original of "
              "this Power of Attorney, unless that third party has actual knowledge it has terminated or is invalid."),
        ("p", "Any third party may deal with my agent in any matters or transactions in the same manner and to the same "
              "extent that the third party would be able to deal with me in any such matters or transactions."),
        ("p", "Any third parties who act in reliance on the representations of my agent shall be held harmless by me, my "
              "estate, the beneficiaries of my estate, and any joint owners of property from any losses actually "
              "suffered or liabilities actually incurred due to actions taken prior to receipt of any written notice of "
              "revocation, suspension, petition to determine my incapacity, termination in part or in whole, or my "
              "death."),
        ("p", "Any action by my agent that is lawfully done as provided in this Power of Attorney is binding on me and my "
              "heirs, legal and personal representatives, and assigns. Any action done for me or on my behalf must be "
              "done in my name. In taking any action for me or on my behalf, my agent shall execute, sign, and endorse "
              "any instruments by writing my name, the name of my agent, and the phrase \"as Agent\" to designate that my "
              "agent is acting for me or on my behalf as my agent. For example:"),
        ("indent", "<b>(My Name) by (My Agent's Name) as Agent,</b>"),
        ("indent", "<b>– OR –</b>"),
        ("indent", "<b>(My Agent's Name) as Agent for (My Name)</b>"),
        ("h1", "Liability of My Agent"),
        ("p", "My agent will not be liable for any acts or decisions made in good faith and made consistent with the "
              "powers, provisions, and limitations contained in this Power of Attorney. My agent is not relieved of "
              "liability for any breach of duty or any acts committed fraudulently, dishonestly, without proper motive, "
              "or with reckless indifference to me or the purposes of this Power of Attorney."),
        ("pagebreak",),
        ("h1", "Signature by Principal"),
        ("spacer", 12),
        ("sig", ["Signed this day of ______________________<br/>Date", f"Signature<br/>{name}<br/>Principal"]),
        ("h1", "Witnesses"),
        ("p", f"This Power of Attorney was signed by the Principal, {name}, on the date indicated above, in our presence. "
              "The Principal requested that we sign below as witnesses. Each of us is now more than 18 years of age and a "
              "competent witness. Each of us believes the Principal is now more than 18 years of age, of sound mind, and "
              "not acting under any duress, constraint, menace, fraud, misrepresentation, or undue influence."),
        ("spacer", 24),
        ("sig", ["First Witness Signature", "Second Witness Signature"]),
        ("sig", ["First Witness Printed Name", "Second Witness Printed Name"]),
        ("sig", ["Date", "Date"]),
        ("h1", "Certificate of Acknowledgment of Notary Public"),
        ("p", c["notary_jurisdiction"]),
        ("p", "County of ______________________________________"),
        ("p", f"On this _____ day of _______________, 20___, before me, ______________________________________________, "
              f"the undersigned notary, personally appeared {name}, who proved to me on the basis of satisfactory "
              "evidence which were _________________________________________________, to be the person whose names are "
              "subscribed to the foregoing instrument and acknowledged he or she signed the foregoing instrument."),
        ("p", "In witness whereof I hereunto set my hand."),
        ("spacer", 20),
        ("sig", ["Signature of Notary Public", "Personalized Seal"]),
        ("sig", ["Printed Name of Notary Public", ""]),
        ("p", "My Commission expires ______________________"),
    ]
    return b


TEMPLATES = {"will": will_blocks, "poa": poa_blocks, "ahcd": ahcd_blocks, "hipaa": hipaa_blocks}
HEADERS = {
    "will": "Last Will & Testament of {name}",
    "poa": "Durable Power of Attorney for {name}",
    "ahcd": "Advance Health Care Directive for {name}",
    "hipaa": "Authorization to Release Medical Information for {name}",
}


# --------------------------------------------------------------------------
# Rendering: blocks -> PDF (reportlab) and blocks -> plain text
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def blocks_to_text(blocks: list) -> str:
    out = []
    for blk in blocks:
        kind = blk[0]
        if kind == "cover":
            out.append("\n".join(blk[1:]))
            out.append("")
        elif kind in ("h1", "h2", "p", "indent", "li"):
            out.append(_TAG_RE.sub("", blk[1]).replace("<br/>", "\n"))
            out.append("")
        elif kind == "field":
            val = _TAG_RE.sub("", blk[2]).replace("<br/>", "\n")
            out.append(f"{blk[1]} {val}".rstrip())
        elif kind == "sig":
            out.append("    ".join(x.replace("<br/>", " / ") for x in blk[1] if x))
            out.append("")
        elif kind == "blank":
            out.extend(["_" * 80] * blk[1])
        elif kind == "pagebreak":
            out.append("\f")
    return "\n".join(out)


def render_pdf(doc: str, ctx: dict, path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        KeepTogether,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    body = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=13.5, leftIndent=12, spaceAfter=8)
    h1 = ParagraphStyle("h1", fontName="Times-Bold", fontSize=14, leading=17, spaceBefore=12, spaceAfter=6)
    h2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=10, leading=13, leftIndent=12, spaceBefore=6, spaceAfter=3)
    indent = ParagraphStyle("indent", parent=body, leftIndent=36, spaceAfter=4)
    li = ParagraphStyle("li", parent=body, leftIndent=36, spaceAfter=6)
    cover_small = ParagraphStyle("cover_small", fontName="Helvetica", fontSize=11, leading=14, textColor=colors.grey)
    cover_big = ParagraphStyle("cover_big", fontName="Times-Bold", fontSize=26, leading=32)
    cover_sub = ParagraphStyle("cover_sub", fontName="Helvetica", fontSize=11, leading=14, spaceBefore=8)
    label = ParagraphStyle("label", fontName="Helvetica-Bold", fontSize=10, leading=13)
    value = ParagraphStyle("value", fontName="Helvetica", fontSize=10, leading=13)
    sigl = ParagraphStyle("sigl", fontName="Helvetica", fontSize=9, leading=12)

    header_text = HEADERS[doc].format(name=ctx["name"])
    date_text = ctx["date"]
    width = letter[0] - 2 * inch

    def on_page(canvas, d):
        canvas.saveState()
        if d.page > 1:
            canvas.setFont("Times-Bold", 9)
            canvas.drawString(inch, letter[1] - 0.6 * inch, header_text)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawCentredString(letter[0] / 2, 0.55 * inch, f"Page {d.page}")
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(letter[0] - inch, 0.55 * inch, date_text)
        canvas.restoreState()

    flow = []
    for blk in TEMPLATES[doc](ctx):
        kind = blk[0]
        if kind == "cover":
            flow.append(Spacer(1, 0.2 * inch))
            flow.append(Paragraph(blk[1], cover_big))
            flow.append(Paragraph(blk[2], cover_big))
            for extra in blk[3:]:
                flow.append(Paragraph(extra, cover_sub))
            flow.append(PageBreak())
        elif kind == "h1":
            flow.append(Paragraph(blk[1], h1))
        elif kind == "h2":
            flow.append(Paragraph(blk[1], h2))
        elif kind == "p":
            flow.append(Paragraph(blk[1], body))
        elif kind == "indent":
            flow.append(Paragraph(blk[1], indent))
        elif kind == "li":
            flow.append(Paragraph(blk[1], li))
        elif kind == "field":
            _, lab, val, lined = blk
            t = Table([[Paragraph(lab, label), Paragraph(val or "&nbsp;", value)]], colWidths=[2.1 * inch, width - 2.1 * inch], hAlign="LEFT")
            style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, 0), 36), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]
            if lined:
                style.append(("LINEBELOW", (1, 0), (1, 0), 0.5, colors.black))
            t.setStyle(TableStyle(style))
            flow.append(t)
        elif kind == "sig":
            labels = blk[1]
            cells = [Paragraph(x, sigl) for x in labels]
            t = Table([cells], colWidths=[width / len(labels)] * len(labels), hAlign="LEFT")
            style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 18)]
            for i, x in enumerate(labels):
                if x:
                    style.append(("LINEABOVE", (i, 0), (i, 0), 0.5, colors.black))
            t.setStyle(TableStyle(style))
            flow.append(KeepTogether(t))
        elif kind == "blank":
            for _ in range(blk[1]):
                t = Table([[""]], colWidths=[width], rowHeights=[20])
                t.setStyle(TableStyle([("LINEBELOW", (0, 0), (0, 0), 0.5, colors.black)]))
                flow.append(t)
        elif kind == "pagebreak":
            flow.append(PageBreak())
        elif kind == "spacer":
            flow.append(Spacer(1, blk[1]))

    path.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(path), pagesize=letter, leftMargin=inch, rightMargin=inch, topMargin=inch, bottomMargin=inch,
        title=header_text, author=ctx["name"],
    ).build(flow, onFirstPage=on_page, onLaterPages=on_page)


def build_all(con: sqlite3.Connection, out: Path) -> list[Path]:
    written = []
    for prow in list_principals(con):
        ctx = load_context(con, prow["id"])
        folder = out / ctx["name"]
        for key, title in DOCS.items():
            pdf = folder / f"{title}.pdf"
            render_pdf(key, ctx, pdf)
            (folder / f"{title}.txt").write_text(blocks_to_text(TEMPLATES[key](ctx)))
            written.append(pdf)
    return written


# --------------------------------------------------------------------------
# Verification against the original PDFs (needs poppler's pdftotext)
# --------------------------------------------------------------------------

_HEADER_PATTERNS = [
    re.compile(r"^\s*(Page\s+)?\d+\s*$"),
    re.compile(r"^\s*trust\s*\S*\s*will\s*$", re.I),
    re.compile(r"^\s*[A-Z][a-z]+ \d{1,2}, \d{4}\s*$"),
    re.compile(r"^\s*(Last Will & Testament of|Durable Power of Attorney for|Advance Health Care Directive for|"
               r"Authorization to Release Medical Information for) .+$"),
    re.compile(r"^\s*(Page\s+\d+\s+)?[A-Z][a-z]+ \d{1,2}, \d{4}\s*$"),
    re.compile(r"^=====.*$"),
    re.compile(r"^\s*for\s*$"),
    re.compile(r"^\s*(Last Will & Testament|HIPAA Authorization|Advance Healthcare Directive|Power of Attorney)\s*$"),
]


def normalize_text(text: str) -> list[str]:
    text = text.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    text = text.replace("–", "-").replace("—", "-").replace("\f", "\n")
    lines = []
    for line in text.splitlines():
        if any(p.match(line) for p in _HEADER_PATTERNS):
            continue
        line = re.sub(r"_+", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    joined = " ".join(lines)
    joined = re.sub(r"(\w)- (\w)", r"\1-\2", joined)
    # split into sentences so the diff is readable regardless of line wrapping
    sentences = re.split(r"(?<=[.;:])\s+", joined)
    return [s.strip() for s in sentences if s.strip()]


def pdf_text(path: Path) -> str:
    if shutil.which("pdftotext") is None:
        raise SystemExit("pdftotext (poppler) is required for verify; brew install poppler")
    return subprocess.run(["pdftotext", "-layout", str(path), "-"], check=True, capture_output=True, text=True).stdout


def verify(con: sqlite3.Connection, originals: Path, out: Path) -> int:
    build_all(con, out)
    total = 0
    for prow in list_principals(con):
        name = prow["full_name"]
        for title in DOCS.values():
            gen = out / name / f"{title}.pdf"
            candidates = [d for d in originals.iterdir() if d.is_dir() and d.name.split()[0] == name.split()[0]]
            if not candidates:
                print(f"== {name} / {title}: no original folder found")
                continue
            ref_txt = candidates[0] / f"{title}.txt"
            ref_pdf = candidates[0] / f"{title}.pdf"
            if ref_txt.exists():
                ref = ref_txt.read_text()
            elif ref_pdf.exists():
                ref = pdf_text(ref_pdf)
            else:
                print(f"== {name} / {title}: no original found")
                continue
            a, b = normalize_text(ref), normalize_text(pdf_text(gen))
            diff = list(difflib.unified_diff(a, b, "original", "generated", lineterm="", n=0))
            changed = [d for d in diff if d[:1] in "+-" and not d.startswith(("+++", "---"))]
            total += len(changed)
            print(f"== {name} / {title}: {len(changed)} differing sentences")
            for d in changed:
                print("   " + d)
    return total


# --------------------------------------------------------------------------
# Web UI (Flask)
# --------------------------------------------------------------------------

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Estate plan</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font:15px/1.45 -apple-system,system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#222}
a{color:#0645ad}nav a{margin-right:14px}
h1{font-size:22px}h2{font-size:17px;margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:4px}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:5px 8px;border-bottom:1px solid #eee;vertical-align:top}
input[type=text],textarea,select{width:100%;box-sizing:border-box;padding:5px;font:inherit}
textarea{min-height:60px}
form.inline{display:inline}button{font:inherit;padding:3px 9px;cursor:pointer}
.row{display:grid;grid-template-columns:200px 1fr;gap:8px 12px;align-items:start;margin:6px 0}
.hint{color:#666;font-size:13px}.ok{background:#e8f5e9;padding:8px 12px;border-radius:4px}
.danger{color:#b00}
</style></head><body>
<nav><a href="/">Principals</a><a href="/people">People</a><a href="/build">Build documents</a></nav>
{% with m = get_flashed_messages() %}{% if m %}<p class="ok">{{ m|join(' ') }}</p>{% endif %}{% endwith %}
{{ body|safe }}
</body></html>"""


def create_app(con_factory):
    from flask import Flask, abort, flash, redirect, render_template_string, request, send_file, url_for
    from markupsafe import escape

    app = Flask(__name__)
    app.secret_key = "estateplan-local"

    def page(body):
        return render_template_string(PAGE, body=body)

    def people_options(con, selected=None):
        opts = []
        for p in con.execute("SELECT id, full_name FROM person ORDER BY full_name"):
            sel = " selected" if selected == p["id"] else ""
            opts.append(f'<option value="{p["id"]}"{sel}>{escape(p["full_name"])}</option>')
        return "".join(opts)

    @app.route("/")
    def index():
        con = con_factory()
        rows = []
        for p in list_principals(con):
            rows.append(f'<li><a href="/principal/{p["id"]}">{escape(p["full_name"])}</a> &middot; '
                        + " ".join(f'<a href="/preview/{p["id"]}/{k}">{escape(t)}</a>' for k, t in DOCS.items())
                        + "</li>")
        body = "<h1>Estate plan</h1><p>Principals (each gets a will, power of attorney, health care directive and HIPAA authorization):</p><ul>" + "".join(rows) + "</ul>"
        body += "<p class='hint'>Preview links render a fresh PDF from the database. <a href='/build'>Build documents</a> writes all eight PDFs to the output folder.</p>"
        return page(body)

    @app.route("/people")
    def people():
        con = con_factory()
        rows = "".join(
            f'<tr><td><a href="/people/{p["id"]}">{escape(p["full_name"])}</a></td><td>{escape(p["address"]).replace(chr(10), "<br>")}</td>'
            f'<td>{escape(p["phone"])}</td><td>{escape(p["email"])}</td><td>{escape(p["birth_date"])}</td></tr>'
            for p in con.execute("SELECT * FROM person ORDER BY full_name")
        )
        body = ("<h1>People</h1><table><tr><th>Name</th><th>Address</th><th>Phone</th><th>Email</th><th>Born</th></tr>"
                + rows + "</table><p><a href='/people/new'>Add a person</a></p>")
        return page(body)

    def person_form(p):
        p = p or {"id": None, "full_name": "", "address": "", "phone": "", "email": "", "birth_date": "", "notes": ""}
        action = "/people/new" if p["id"] is None else f"/people/{p['id']}"
        f = f'<form method="post" action="{action}">'
        for key, lab in (("full_name", "Full legal name"), ("phone", "Phone"), ("email", "Email"), ("birth_date", "Birth date (YYYY-MM-DD)"), ("notes", "Notes")):
            f += f'<div class="row"><label>{lab}</label><input type="text" name="{key}" value="{escape(p[key])}"></div>'
        f += f'<div class="row"><label>Address<br><span class="hint">one line per printed line</span></label><textarea name="address">{escape(p["address"])}</textarea></div>'
        f += '<div class="row"><span></span><span><button type="submit">Save</button></span></div></form>'
        if p["id"] is not None:
            f += f'<form method="post" action="/people/{p["id"]}/delete" onsubmit="return confirm(\'Delete this person and all their roles?\')"><button class="danger">Delete person</button></form>'
        return f

    @app.route("/people/new", methods=["GET", "POST"])
    def person_new():
        con = con_factory()
        if request.method == "POST":
            con.execute("INSERT INTO person(full_name,address,phone,email,birth_date,notes) VALUES (?,?,?,?,?,?)",
                        tuple(request.form.get(k, "").strip() for k in ("full_name", "address", "phone", "email", "birth_date", "notes")))
            con.commit()
            flash("Person added.")
            return redirect("/people")
        return page("<h1>Add a person</h1>" + person_form(None))

    @app.route("/people/<int:pid>", methods=["GET", "POST"])
    def person_edit(pid):
        con = con_factory()
        p = con.execute("SELECT * FROM person WHERE id=?", (pid,)).fetchone()
        if p is None:
            abort(404)
        if request.method == "POST":
            con.execute("UPDATE person SET full_name=?,address=?,phone=?,email=?,birth_date=?,notes=? WHERE id=?",
                        tuple(request.form.get(k, "").strip() for k in ("full_name", "address", "phone", "email", "birth_date", "notes")) + (pid,))
            con.commit()
            flash("Saved.")
            return redirect("/people")
        return page(f"<h1>{escape(p['full_name'])}</h1>" + person_form(dict(p)))

    @app.route("/people/<int:pid>/delete", methods=["POST"])
    def person_delete(pid):
        con = con_factory()
        used = con.execute("SELECT COUNT(*) FROM principal WHERE person_id=? OR spouse_id=?", (pid, pid)).fetchone()[0]
        if used:
            flash("That person is a principal or spouse; change the principal first.")
            return redirect(f"/people/{pid}")
        con.execute("DELETE FROM role WHERE person_id=?", (pid,))
        con.execute("DELETE FROM gift WHERE recipient_id=?", (pid,))
        con.execute("DELETE FROM person WHERE id=?", (pid,))
        con.commit()
        flash("Deleted.")
        return redirect("/people")

    @app.route("/principal/<int:pid>", methods=["GET", "POST"])
    def principal(pid):
        con = con_factory()
        if request.method == "POST":
            f = request.form
            con.execute(
                "UPDATE principal SET spouse_id=?, state_name=?, notary_jurisdiction=?, poa_statute=?, residence_line=?, remains=?, "
                "ceremony=?, final_special_request=?, care_preference=?, organ_donation=?, hc_special_instructions=?, "
                "poa_special_instructions=? WHERE id=?",
                (f.get("spouse_id") or None, f.get("state_name", "").strip(), f.get("notary_jurisdiction", "").strip(),
                 f.get("poa_statute", "").strip(), f.get("residence_line", "").strip(),
                 f.get("remains_other", "").strip() if f.get("remains") == "other" else f.get("remains", ""),
                 f.get("ceremony_other", "").strip() if f.get("ceremony") == "other" else f.get("ceremony", ""),
                 f.get("final_special_request", "").strip(), f.get("care_preference", "improve_only"),
                 1 if f.get("organ_donation") else 0, f.get("hc_special_instructions", "").strip(),
                 f.get("poa_special_instructions", "").strip(), pid),
            )
            con.commit()
            flash("Saved.")
            return redirect(f"/principal/{pid}")
        c = load_context(con, pid)
        remains_std = c["remains"] in ("cremated", "buried", "")
        ceremony_std = c["ceremony"] in ("executor", "")
        b = f"<h1>{escape(c['name'])}</h1>"
        b += "<p>" + " ".join(f'<a href="/preview/{pid}/{k}">Preview {escape(t)}</a>' for k, t in DOCS.items()) + "</p>"
        b += f'<form method="post"><h2>Choices</h2>'
        b += f'<div class="row"><label>Spouse</label><select name="spouse_id"><option value="">(none)</option>{people_options(con, c["spouse_id"])}</select></div>'
        b += f'<div class="row"><label>State (will, directive)</label><input type="text" name="state_name" value="{escape(c["state_name"])}"></div>'
        b += f'<div class="row"><label>Notary jurisdiction line</label><input type="text" name="notary_jurisdiction" value="{escape(c["notary_jurisdiction"])}"></div>'
        b += f'<div class="row"><label>POA statute (cover)</label><input type="text" name="poa_statute" value="{escape(c["poa_statute"])}"></div>'
        b += f'<div class="row"><label>Residence, one line (POA)</label><input type="text" name="residence_line" value="{escape(c["residence_line"])}"></div>'
        b += ('<div class="row"><label>Remains (will, Final Arrangements)</label><span>'
              f'<label><input type="radio" name="remains" value="cremated" {"checked" if c["remains"]=="cremated" else ""}> Cremated</label> &nbsp;'
              f'<label><input type="radio" name="remains" value="buried" {"checked" if c["remains"]=="buried" else ""}> Buried</label> &nbsp;'
              f'<label><input type="radio" name="remains" value="" {"checked" if c["remains"]=="" else ""}> No direction</label> &nbsp;'
              f'<label><input type="radio" name="remains" value="other" {"" if remains_std else "checked"}> Other:</label> '
              f'<input type="text" name="remains_other" value="{"" if remains_std else escape(c["remains"])}" placeholder="my body be donated to science."></span></div>')
        b += ('<div class="row"><label>Ceremony</label><span>'
              f'<label><input type="radio" name="ceremony" value="executor" {"checked" if c["ceremony"]=="executor" else ""}> Let the executor decide</label> &nbsp;'
              f'<label><input type="radio" name="ceremony" value="" {"checked" if c["ceremony"]=="" else ""}> No direction</label> &nbsp;'
              f'<label><input type="radio" name="ceremony" value="other" {"" if ceremony_std else "checked"}> Other:</label> '
              f'<input type="text" name="ceremony_other" value="{"" if ceremony_std else escape(c["ceremony"])}" placeholder="a memorial service be held."></span></div>')
        b += f'<div class="row"><label>Final arrangements special request</label><textarea name="final_special_request">{escape(c["final_special_request"])}</textarea></div>'
        b += ('<div class="row"><label>Care preference (directive)</label><span>'
              f'<label><input type="radio" name="care_preference" value="improve_only" {"checked" if c["care_preference"]=="improve_only" else ""}> Receive care only if it will improve my condition</label><br>'
              f'<label><input type="radio" name="care_preference" value="prolong" {"checked" if c["care_preference"]=="prolong" else ""}> Prolong life as long as possible</label>'
              '<br><span class="hint">Only the first option uses Trust &amp; Will wording.</span></span></div>')
        b += f'<div class="row"><label>Organ donation</label><label><input type="checkbox" name="organ_donation" {"checked" if c["organ_donation"] else ""}> Agent may make anatomical gifts</label></div>'
        b += f'<div class="row"><label>Health care special instructions</label><textarea name="hc_special_instructions">{escape(c["hc_special_instructions"])}</textarea></div>'
        b += f'<div class="row"><label>POA special instructions<br><span class="hint">blank prints ruled lines</span></label><textarea name="poa_special_instructions">{escape(c["poa_special_instructions"])}</textarea></div>'
        b += '<div class="row"><span></span><span><button type="submit">Save choices</button></span></div></form>'
        for role, label_, hint in ROLES:
            b += f"<h2>{label_}</h2><p class='hint'>{hint}</p><table>"
            people = c["roles"].get(role, [])
            for i, p in enumerate(people):
                b += (f'<tr><td>{i+1}.</td><td>{escape(p["full_name"])}</td><td style="white-space:nowrap">'
                      f'<form class="inline" method="post" action="/role/{p["role_id"]}/move/up"><button {"disabled" if i==0 else ""}>&uarr;</button></form> '
                      f'<form class="inline" method="post" action="/role/{p["role_id"]}/move/down"><button {"disabled" if i==len(people)-1 else ""}>&darr;</button></form> '
                      f'<form class="inline" method="post" action="/role/{p["role_id"]}/delete"><button>remove</button></form></td></tr>')
            b += (f'<tr><td></td><td><form class="inline" method="post" action="/principal/{pid}/role/{role}">'
                  f'<select name="person_id">{people_options(con)}</select> <button>Add</button></form></td><td></td></tr></table>')
        b += "<h2>Specific gifts</h2><table>"
        for g in c["gifts"]:
            b += (f'<tr><td>To {escape(g["recipient"])}, I give {escape(g["item"])}</td><td style="white-space:nowrap">'
                  f'<form class="inline" method="post" action="/gift/{g["gift_id"]}/delete"><button>remove</button></form></td></tr>')
        b += (f'<tr><td><form class="inline" method="post" action="/principal/{pid}/gift">'
              f'<select name="recipient_id" style="width:40%">{people_options(con)}</select> '
              f'<input type="text" name="item" placeholder="Item" style="width:40%"> <button>Add gift</button></form></td><td></td></tr></table>')
        return page(b)

    @app.route("/principal/<int:pid>/role/<role>", methods=["POST"])
    def role_add(pid, role):
        con = con_factory()
        if role not in ROLE_LABEL:
            abort(404)
        pos = con.execute("SELECT COALESCE(MAX(position),-1)+1 FROM role WHERE principal_id=? AND role=?", (pid, role)).fetchone()[0]
        con.execute("INSERT INTO role(principal_id,role,person_id,position) VALUES (?,?,?,?)", (pid, role, int(request.form["person_id"]), pos))
        con.commit()
        return redirect(f"/principal/{pid}")

    @app.route("/role/<int:rid>/move/<direction>", methods=["POST"])
    def role_move(rid, direction):
        con = con_factory()
        r = con.execute("SELECT * FROM role WHERE id=?", (rid,)).fetchone()
        if r is None:
            abort(404)
        rows = con.execute("SELECT id FROM role WHERE principal_id=? AND role=? ORDER BY position", (r["principal_id"], r["role"])).fetchall()
        ids = [x["id"] for x in rows]
        i = ids.index(rid)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            for pos, x in enumerate(ids):
                con.execute("UPDATE role SET position=? WHERE id=?", (pos, x))
            con.commit()
        return redirect(f"/principal/{r['principal_id']}")

    @app.route("/role/<int:rid>/delete", methods=["POST"])
    def role_delete(rid):
        con = con_factory()
        r = con.execute("SELECT principal_id FROM role WHERE id=?", (rid,)).fetchone()
        if r is None:
            abort(404)
        con.execute("DELETE FROM role WHERE id=?", (rid,))
        con.commit()
        return redirect(f"/principal/{r['principal_id']}")

    @app.route("/principal/<int:pid>/gift", methods=["POST"])
    def gift_add(pid):
        con = con_factory()
        item = request.form.get("item", "").strip()
        if item:
            pos = con.execute("SELECT COALESCE(MAX(position),-1)+1 FROM gift WHERE principal_id=?", (pid,)).fetchone()[0]
            con.execute("INSERT INTO gift(principal_id,recipient_id,item,position) VALUES (?,?,?,?)", (pid, int(request.form["recipient_id"]), item, pos))
            con.commit()
        return redirect(f"/principal/{pid}")

    @app.route("/gift/<int:gid>/delete", methods=["POST"])
    def gift_delete(gid):
        con = con_factory()
        g = con.execute("SELECT principal_id FROM gift WHERE id=?", (gid,)).fetchone()
        if g is None:
            abort(404)
        con.execute("DELETE FROM gift WHERE id=?", (gid,))
        con.commit()
        return redirect(f"/principal/{g['principal_id']}")

    @app.route("/preview/<int:pid>/<doc>")
    def preview(pid, doc):
        if doc not in DOCS:
            abort(404)
        con = con_factory()
        ctx = load_context(con, pid)
        path = OUT_DIR / "_preview" / ctx["name"] / f"{DOCS[doc]}.pdf"
        render_pdf(doc, ctx, path)
        return send_file(path, mimetype="application/pdf", max_age=0)

    @app.route("/build", methods=["GET", "POST"])
    def build():
        con = con_factory()
        if request.method == "POST":
            out = OUT_DIR / dt.date.today().isoformat()
            written = build_all(con, out)
            links = "".join(f'<li><a href="/output/{escape(str(p.relative_to(OUT_DIR)))}">{escape(str(p.relative_to(out)))}</a></li>' for p in written)
            return page(f"<h1>Built {len(written)} documents</h1><p>Folder: <code>{escape(str(out))}</code></p><ul>{links}</ul>")
        return page("<h1>Build documents</h1><p>Writes every principal's four documents (PDF plus a plain-text copy) into a dated folder.</p>"
                    "<form method='post'><button type='submit'>Build now</button></form>")

    @app.route("/output/<path:rel>")
    def output(rel):
        path = (OUT_DIR / rel).resolve()
        if OUT_DIR.resolve() not in path.parents or not path.exists():
            abort(404)
        return send_file(path, max_age=0)

    return app


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv=None):
    global DB_PATH
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=5077)
    s.add_argument("--host", default="127.0.0.1")
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, default=None)
    t = sub.add_parser("text")
    t.add_argument("principal", help="principal id or first name")
    t.add_argument("doc", choices=list(DOCS))
    v = sub.add_parser("verify")
    v.add_argument("originals", type=Path)
    v.add_argument("--out", type=Path, default=OUT_DIR / "_verify")
    sub.add_parser("reseed", help="wipe the database and reload family.local.json (or the built-in Doe family)")
    e = sub.add_parser("export", help="print the database as JSON (redirect to family.local.json)")
    e.add_argument("--out", type=Path, default=None)
    i = sub.add_parser("import", help="replace the database contents with a JSON file")
    i.add_argument("file", type=Path)
    args = ap.parse_args(argv)

    DB_PATH = args.db
    cmd = args.cmd or "serve"

    if cmd == "reseed":
        if DB_PATH.exists():
            DB_PATH.unlink()
        connect(DB_PATH)
        print(f"reseeded {DB_PATH}")
        return

    con = connect(DB_PATH)
    if cmd == "serve":
        app = create_app(lambda: connect(DB_PATH))
        print(f"estate plan editor: http://{args.host}:{args.port}/")
        app.run(host=args.host, port=args.port, debug=False)
    elif cmd == "build":
        out = args.out or OUT_DIR / dt.date.today().isoformat()
        for p in build_all(con, out):
            print(p)
    elif cmd == "text":
        pid = None
        for row in list_principals(con):
            if str(row["id"]) == args.principal or row["full_name"].lower().startswith(args.principal.lower()):
                pid = row["id"]
        if pid is None:
            raise SystemExit("unknown principal")
        print(blocks_to_text(TEMPLATES[args.doc](load_context(con, pid))))
    elif cmd == "verify":
        n = verify(con, args.originals, args.out)
        sys.exit(1 if n else 0)
    elif cmd == "export":
        text = json.dumps(dump_data(con), indent=2, ensure_ascii=False) + "\n"
        if args.out:
            args.out.write_text(text)
            print(f"wrote {args.out}")
        else:
            sys.stdout.write(text)
    elif cmd == "import":
        load_data(con, json.loads(args.file.read_text()))
        print(f"loaded {args.file} into {DB_PATH}")


if __name__ == "__main__":
    main()
