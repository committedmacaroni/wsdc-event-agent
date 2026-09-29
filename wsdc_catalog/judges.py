"""Judge identity resolution: decide when differently written judge names are the same person.

Evidence and rules (deterministic; every decision records its method, confidence and reason):
  - Hard rule: two names that appear on the SAME panel (same sheet section) are different
    people and are never merged.
  - high:   same first + last name, middle name/initial dropped ("Lisa M Picard" = "Lisa Picard")
  - high:   same last name, first names are known nickname forms ("Mike Smith" = "Michael Smith")
  - medium: same last name, one first name is a prefix of the other ("Chris" / "Christopher")
  - medium: a first-name-only entry ("Tren", "Naomi") matching exactly ONE full-named judge,
            by first name or first-name prefix
  - review: a first-name-only entry matching several full-named judges -> not merged, listed
            with candidates for a person to decide
  - manual: admin merge/separate overrides, applied before everything else
Accents, case and punctuation are already ignored by normalize_text.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from .db import iso, utcnow
from .normalize import normalize_text

_NICKNAME_GROUPS = [
    ("michael", "mike", "mikey", "mick"), ("christopher", "chris", "topher"), ("christina", "chris", "christine", "tina"),
    ("jennifer", "jen", "jenn", "jenny"), ("jessica", "jess", "jessie"), ("katherine", "kate", "katie", "kat", "kathy"),
    ("catherine", "cat", "cathy", "kate", "katie"), ("elizabeth", "liz", "beth", "lizzy", "libby", "eliza", "betsy"),
    ("robert", "rob", "bob", "bobby", "robbie"), ("william", "will", "bill", "billy", "liam"),
    ("richard", "rich", "rick", "ricky"), ("james", "jim", "jimmy", "jamie"), ("joseph", "joe", "joey"),
    ("daniel", "dan", "danny"), ("danielle", "dani"), ("matthew", "matt"), ("nicholas", "nick", "nicky"),
    ("nicole", "nicki", "nikki", "nic"), ("anthony", "tony"), ("alexander", "alex", "xander"),
    ("alexandra", "alex", "lexi", "sandra"), ("benjamin", "ben", "benny"), ("samuel", "sam", "sammy"),
    ("samantha", "sam", "sammy"), ("jonathan", "jon", "jonny"), ("john", "jack", "johnny"),
    ("patricia", "patty", "pat", "trish", "tricia"), ("patrick", "pat", "paddy"), ("margaret", "maggie", "meg", "peggy"),
    ("rebecca", "becca", "becky"), ("victoria", "tori", "vicky"), ("andrew", "andy", "drew"),
    ("steven", "steve"), ("stephen", "steve"), ("thomas", "tom", "tommy"), ("timothy", "tim"),
    ("edward", "ed", "eddie", "ted"), ("theodore", "ted", "theo"), ("gregory", "greg"), ("jeffrey", "jeff"),
    ("kenneth", "ken", "kenny"), ("ronald", "ron"), ("donald", "don"), ("lawrence", "larry"),
    ("zachary", "zach", "zack"), ("jacqueline", "jackie"), ("deborah", "deb", "debbie"), ("susan", "sue", "susie"),
    ("gabriel", "gabe"), ("gabrielle", "gabby", "gabi"), ("nathaniel", "nate", "nathan"), ("nathan", "nate"),
    ("isabella", "bella", "izzy"), ("isabel", "bella", "izzy"), ("arjay", "aj", "rj"), ("frances", "fran"),
    ("francesca", "fran", "frankie"), ("francis", "fran", "frank"), ("franklin", "frank"),
    ("abigail", "abby", "abbie"), ("amanda", "mandy"), ("melissa", "mel", "missy"), ("melanie", "mel"),
    ("valerie", "val"), ("veronica", "ronnie"), ("jacob", "jake"), ("joshua", "josh"), ("kimberly", "kim"),
    ("vincent", "vince", "vinny"), ("leonard", "leo", "len", "lenny"), ("trendlyon", "tren"),
]
_NICK = defaultdict(set)
for i, grp in enumerate(_NICKNAME_GROUPS):
    for n in grp:
        _NICK[n].add(i)
MIN_PREFIX = 3


def _nickname(a: str, b: str) -> bool:
    return bool(_NICK.get(a, set()) & _NICK.get(b, set()))


def _prefix(a: str, b: str) -> bool:
    short, long_ = sorted((a, b), key=len)
    return len(short) >= MIN_PREFIX and long_.startswith(short) and short != long_


def _first_compatible(a: str, b: str) -> str | None:
    if a == b:
        return "same"
    if _nickname(a, b):
        return "nickname"
    if _prefix(a, b):
        return "prefix"
    return None


def _panels(conn) -> tuple[list[set], dict]:
    """Every judging panel (sheet + section) as a set of normalized judge names, and display forms."""
    panels: dict = defaultdict(set)
    shown: dict = defaultdict(Counter)
    for r in conn.execute("SELECT sheet_url, section, marks FROM scoresheet_entries WHERE marks LIKE '{%'"):
        try:
            names = json.loads(r["marks"]).keys()
        except (ValueError, AttributeError):
            continue
        for n in names:
            norm = normalize_text(n)
            if norm:
                panels[(r["sheet_url"], r["section"])].add(norm)
                shown[norm][n] += 1
    return list(panels.values()), shown


def rebuild(conn) -> dict:
    """Recompute judge identities from every indexed sheet. Returns a short report."""
    panels, shown = _panels(conn)
    names = sorted(shown)
    panel_count = Counter(n for p in panels for n in p)
    together = set()
    for p in panels:
        for a in p:
            for b in p:
                if a < b:
                    together.add((a, b))

    parent = {n: n for n in names}
    how: dict = {n: ("exact", "high", None) for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    members = lambda root: [n for n in names if find(n) == root]

    def conflicts(a, b) -> bool:
        ma, mb = members(find(a)), members(find(b))
        return any((min(x, y), max(x, y)) in together or (min(x, y), max(x, y)) in blocked for x in ma for y in mb)

    def union(a, b, method, conf, reason):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        parent[rb] = ra
        for n in (a, b):
            if how[n][0] == "exact":
                how[n] = (method, conf, reason)

    # manual overrides first
    blocked = set()
    for o in conn.execute("SELECT * FROM judge_overrides ORDER BY id"):
        a, b = normalize_text(o["name_a"]), normalize_text(o["name_b"])
        if a in parent and b in parent:
            if o["action"] == "separate":
                blocked.add((min(a, b), max(a, b)))
            else:
                union(a, b, "manual", "manual", "merged by an admin")

    toks = {n: n.split() for n in names}
    full = [n for n in names if len(toks[n]) >= 2]
    single = [n for n in names if len(toks[n]) == 1]

    # full names: same last name + compatible first name
    for i, a in enumerate(full):
        for b in full[i + 1:]:
            ta, tb = toks[a], toks[b]
            if ta[-1] != tb[-1]:
                continue
            kind = _first_compatible(ta[0], tb[0])
            if not kind or conflicts(a, b):
                continue
            if kind == "same":
                union(a, b, "middle_name", "high", "same first and last name; middle name or initial differs")
            elif kind == "nickname":
                union(a, b, "nickname", "high", f"'{ta[0]}' and '{tb[0]}' are forms of the same first name")
            else:
                union(a, b, "first_name_prefix", "medium", f"'{ta[0]}' / '{tb[0]}' with the same last name")

    # first-name-only entries
    review = {}
    for s_ in single:
        roots = {}
        for f in full:
            if _first_compatible(s_, toks[f][0]) and not conflicts(s_, f):
                roots.setdefault(find(f), f)
        if len(roots) == 1:
            f = next(iter(roots.values()))
            union(f, s_, "first_name_only", "medium",
                  f"'{shown[s_].most_common(1)[0][0]}' matches only one full-named judge, and never judged "
                  f"on the same panel")
        elif len(roots) > 1:
            review[s_] = sorted(shown[f].most_common(1)[0][0] for f in roots.values())

    # canonical name per cluster: most name parts, then most panels
    clusters = defaultdict(list)
    for n in names:
        clusters[find(n)].append(n)
    ts = iso(utcnow())
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM judge_aliases")
        for mem in clusters.values():
            best = max(mem, key=lambda n: (len(toks[n]), panel_count[n], n))
            display = shown[best].most_common(1)[0][0]
            for n in mem:
                method, conf, reason = how[n] if len(mem) > 1 else ("exact", "high", None)
                if n in review and len(mem) == 1:
                    method, conf, reason = "needs_review", "low", "first name matches several judges"
                conn.execute(
                    "INSERT INTO judge_aliases (alias_norm, alias_name, judge_key, judge_name, method, confidence, "
                    "reason, candidates, panels, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (n, shown[n].most_common(1)[0][0], best, display, method, conf, reason,
                     json.dumps(review[n]) if n in review else None, panel_count[n], ts))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    merged = sum(1 for m in clusters.values() if len(m) > 1)
    return {"judge_names": len(names), "judges": len(clusters), "merged_groups": merged,
            "needs_review": len(review)}


def alias_map(conn) -> dict:
    """normalized judge name -> identity row. Rebuilds lazily if sheets exist but no identities do."""
    rows = conn.execute("SELECT * FROM judge_aliases").fetchall()
    if not rows and conn.execute("SELECT 1 FROM scoresheet_entries LIMIT 1").fetchone():
        rebuild(conn)
        rows = conn.execute("SELECT * FROM judge_aliases").fetchall()
    return {r["alias_norm"]: dict(r) for r in rows}


def list_judges(conn, review_only: bool = False) -> dict:
    by = defaultdict(list)
    for r in alias_map(conn).values():
        by[r["judge_key"]].append(r)
    judges, review = [], []
    for key, rows in sorted(by.items(), key=lambda kv: kv[1][0]["judge_name"].lower()):
        entry = {"judge": rows[0]["judge_name"], "judge_key": key,
                 "panels": sum(r["panels"] for r in rows),
                 "names": [{"name": r["alias_name"], "method": r["method"], "confidence": r["confidence"],
                            "reason": r["reason"], "panels": r["panels"]} for r in rows]}
        for r in rows:
            if r["method"] == "needs_review":
                review.append({"name": r["alias_name"], "candidates": json.loads(r["candidates"] or "[]"),
                               "panels": r["panels"]})
        if not review_only:
            judges.append(entry)
    return {"judges": judges, "needs_review": review}


def add_override(conn, action: str, name_a: str, name_b: str) -> dict:
    if action not in ("merge", "separate"):
        raise ValueError("action must be merge or separate")
    if not normalize_text(name_a) or not normalize_text(name_b):
        raise ValueError("two judge names are required")
    conn.execute("INSERT INTO judge_overrides (action, name_a, name_b, created_at) VALUES (?,?,?,?)",
                 (action, name_a, name_b, iso(utcnow())))
    return rebuild(conn)
