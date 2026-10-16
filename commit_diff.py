#!/usr/bin/env python3
"""
commit_diff.py — Show every commit on a GitHub repo's main branch since a date,
with the added, removed and changed lines and who made each commit, as an
interactive HTML report.

Usage:
    python3 commit_diff.py <repo> <since> [-b BRANCH] [-o OUT.html] [--no-open]

    <repo>   https://github.com/owner/name, git@github.com:owner/name.git or owner/name
    <since>  YYYY-MM-DD (or YYYY-MM-DDTHH:MM); commits from that moment until now

Run with no arguments to be prompted for both.
Needs only Python 3.8+ and git. Private repos work if your git credentials do.
"""
import argparse
import datetime as dt
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

REC, FLD, END = "\x1e", "\x1f", "\x1d"
MAX_LINES = 400            # lines kept per category per file in the report
MAX_INLINE_LEN = 2000      # skip word-level highlighting on longer lines
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class GitError(RuntimeError):
    pass


def git(args, cwd=None):
    p = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=cwd, capture_output=True)
    if p.returncode != 0:
        raise GitError(p.stderr.decode("utf-8", "replace").strip())
    return p.stdout.decode("utf-8", "replace")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- inputs

def normalize_repo(s):
    """Return (clone_url, 'owner/name' or None)."""
    s = s.strip().rstrip("/")
    if re.fullmatch(r"[\w.-]+/[\w.-]+", s):
        s = "https://github.com/" + s
    m = re.match(r"^(?:https?://(?:www\.)?github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/.*)?$", s)
    if not m:
        return s, None
    slug = f"{m[1]}/{m[2]}"
    url = s if s.startswith("git@") else f"https://github.com/{slug}.git"
    return url, slug


def parse_since(s):
    s = s.strip()
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        sys.exit(f"Could not read date '{s}'. Use YYYY-MM-DD, e.g. 2026-09-01.")


# ---------------------------------------------------------------- git

def clone(url, branch, since, dest):
    """Shallow-clone only the history we need. Returns False if nothing is newer than `since`."""
    try:
        git(["clone", "--quiet", "--no-tags", "--single-branch", "--branch", branch,
             f"--shallow-since={since}", url, dest])
    except GitError as e:
        if "shallow" in str(e).lower():
            # git refuses a shallow-since clone that would select no commits
            return False
        raise
    try:
        # Fetch one extra generation so the oldest commit diffs against its real parent
        git(["fetch", "--quiet", "--deepen=1", "origin", branch], cwd=dest)
    except GitError:
        pass
    return True


def read_commits(repo_dir, since):
    fmt = REC + FLD.join(["%H", "%an", "%ae", "%aI", "%cn", "%ce", "%cI", "%P", "%s", "%b"]) + END
    out = git(["log", "HEAD", f"--since={since}", f"--format={fmt}", "-p", "-M",
               "--unified=0", "--no-color", "--no-ext-diff"], cwd=repo_dir)
    commits = []
    for rec in out.split(REC)[1:]:
        header, _, patch = rec.partition(END)
        sha, an, ae, ad, cn, ce, cd, parents, subject, body = header.split(FLD, 9)
        files = parse_patch(patch)
        commits.append({
            "sha": sha,
            "author": {"name": an, "email": ae, "date": ad},
            "committer": {"name": cn, "email": ce, "date": cd},
            "merge": len(parents.split()) > 1,
            "subject": subject,
            "body": body.strip(),
            "files": files,
            "stats": {k: sum(f["counts"][k] for f in files) for k in ("added", "removed", "changed")},
        })
    return commits


# ---------------------------------------------------------------- diff parsing

def tokens(s):
    return re.findall(r"\w+|\s+|[^\w\s]", s)


def inline_diff(a, b):
    """Word-level diff of two lines -> ([[highlight, text], ...] for old, same for new)."""
    if len(a) > MAX_INLINE_LEN or len(b) > MAX_INLINE_LEN:
        return [[1, a]], [[1, b]]
    ta, tb = tokens(a), tokens(b)
    old, new = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if tag == "equal":
            old.append([0, "".join(ta[i1:i2])])
            new.append([0, "".join(tb[j1:j2])])
        else:
            if i2 > i1:
                old.append([1, "".join(ta[i1:i2])])
            if j2 > j1:
                new.append([1, "".join(tb[j1:j2])])
    return old, new


def new_file(line):
    m = re.match(r"diff --git a/(.*) b/(.*)$", line)
    old, new = (m[1], m[2]) if m else ("", "")
    return {"path": new, "old_path": old, "status": "modified", "binary": False,
            "counts": {"added": 0, "removed": 0, "changed": 0},
            "added": [], "removed": [], "changed": []}


def add_block(f, minus, plus):
    """A run of '-' lines followed by '+' lines: pair them up as changed, the rest are pure adds/removes."""
    k = min(len(minus), len(plus))
    for (oln, otext), (nln, ntext) in zip(minus[:k], plus[:k]):
        f["counts"]["changed"] += 1
        if len(f["changed"]) < MAX_LINES:
            o, n = inline_diff(otext, ntext)
            f["changed"].append({"old_ln": oln, "new_ln": nln, "old": o, "new": n})
    for ln, text in minus[k:]:
        f["counts"]["removed"] += 1
        if len(f["removed"]) < MAX_LINES:
            f["removed"].append([ln, text])
    for ln, text in plus[k:]:
        f["counts"]["added"] += 1
        if len(f["added"]) < MAX_LINES:
            f["added"].append([ln, text])


def parse_patch(patch):
    files, f = [], None
    minus, plus = [], []
    in_hunk = False
    old_ln = new_ln = 0

    def flush():
        if f is not None and (minus or plus):
            add_block(f, minus, plus)
        minus.clear()
        plus.clear()

    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            flush()
            f = new_file(line)
            files.append(f)
            in_hunk = False
            continue
        if f is None:
            continue
        if line.startswith("@@"):
            flush()
            m = HUNK.match(line)
            old_ln, new_ln = int(m[1]), int(m[2])
            in_hunk = True
            continue
        if not in_hunk:
            if line.startswith("new file mode"):
                f["status"] = "added"
            elif line.startswith("deleted file mode"):
                f["status"] = "deleted"
            elif line.startswith("rename from "):
                f["old_path"], f["status"] = line[12:], "renamed"
            elif line.startswith("rename to "):
                f["path"] = line[10:]
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                f["binary"] = True
            elif line.startswith("--- ") and line[4:] != "/dev/null":
                f["old_path"] = line[6:] if line.startswith("--- a/") else line[4:]
            elif line.startswith("+++ ") and line[4:] != "/dev/null":
                f["path"] = line[6:] if line.startswith("+++ b/") else line[4:]
            continue
        if line.startswith("-"):
            if plus:          # a '-' after '+' starts a new block
                flush()
            minus.append((old_ln, line[1:]))
            old_ln += 1
        elif line.startswith("+"):
            plus.append((new_ln, line[1:]))
            new_ln += 1
    flush()
    for f in files:
        if f["status"] == "deleted":
            f["path"] = f["old_path"]
    return files


# ---------------------------------------------------------------- report

def build_report(data):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", payload)


def main():
    ap = argparse.ArgumentParser(description="Report all commits on a GitHub repo's main branch since a date.")
    ap.add_argument("repo", nargs="?", help="GitHub URL or owner/name")
    ap.add_argument("since", nargs="?", help="start date, YYYY-MM-DD")
    ap.add_argument("-b", "--branch", default="main", help="branch to read (default: main)")
    ap.add_argument("-o", "--out", help="output HTML file (default: ./commits-<repo>-since-<date>.html)")
    ap.add_argument("--no-open", action="store_true", help="don't open the report in a browser")
    a = ap.parse_args()

    if not shutil.which("git"):
        sys.exit("git is not installed or not on PATH.")
    repo = a.repo or input("GitHub repo (URL or owner/name): ")
    since_dt = parse_since(a.since or input("Since date (YYYY-MM-DD): "))
    since = since_dt.strftime("%Y-%m-%d %H:%M:%S")
    url, slug = normalize_repo(repo)

    with tempfile.TemporaryDirectory(prefix="commit-diff-") as tmp:
        log(f"Fetching {slug or url} ({a.branch}) since {since_dt:%Y-%m-%d %H:%M} …")
        try:
            has = clone(url, a.branch, since, str(Path(tmp, "repo")))
            commits = read_commits(str(Path(tmp, "repo")), since) if has else []
        except GitError as e:
            sys.exit(f"git failed:\n{e}")

    log(f"Found {len(commits)} commit(s).")
    data = {
        "repo": slug or url,
        "repo_url": f"https://github.com/{slug}" if slug else None,
        "branch": a.branch,
        "since": since_dt.isoformat(),
        "generated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "max_lines": MAX_LINES,
        "commits": commits,
    }
    name = (slug or "repo").replace("/", "-")
    out = Path(a.out or f"commits-{name}-since-{since_dt:%Y-%m-%d}.html").resolve()
    out.write_text(build_report(data), encoding="utf-8")
    log(f"Report written to {out}")
    if not a.no_open:
        webbrowser.open(out.as_uri())


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Commit Changes</title>
<style>
:root{
  --bg:#f6f7f9;--panel:#fff;--panel-2:#f9fafb;--border:#e2e5e9;--text:#1c2128;--muted:#656d76;
  --accent:#0969da;--add:#1a7f37;--add-bg:#e9fbee;--add-hl:#aceebb;
  --del:#cf222e;--del-bg:#ffeef0;--del-hl:#ffcdd2;--chg:#9a6700;--chg-bg:#fff6d6;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --shadow:0 1px 2px rgba(0,0,0,.05),0 1px 8px rgba(0,0,0,.04);
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0d1117;--panel:#161b22;--panel-2:#1c222b;--border:#30363d;--text:#e6edf3;--muted:#8b949e;
  --accent:#58a6ff;--add:#3fb950;--add-bg:rgba(46,160,67,.13);--add-hl:rgba(46,160,67,.42);
  --del:#f85149;--del-bg:rgba(248,81,73,.13);--del-hl:rgba(248,81,73,.42);--chg:#d29922;--chg-bg:rgba(187,128,9,.16);
  --shadow:none;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1280px;margin:0 auto;padding:28px 20px 60px}
header h1{margin:0 0 4px;font-size:24px;letter-spacing:-.01em}
header .sub{color:var(--muted)}
header code{font-family:var(--mono);font-size:12.5px;background:var(--panel-2);border:1px solid var(--border);padding:1px 6px;border-radius:6px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:22px 0}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow)}
.stat .n{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}
.stat .l{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}
.stat.add .n{color:var(--add)}.stat.del .n{color:var(--del)}.stat.chg .n{color:var(--chg)}
h2{font-size:15px;margin:26px 0 10px}
.people{display:flex;flex-wrap:wrap;gap:8px}
.person{display:flex;align-items:center;gap:8px;background:var(--panel);border:1px solid var(--border);border-radius:999px;padding:4px 12px 4px 4px;cursor:pointer;user-select:none;font:inherit;color:inherit}
.person.on{border-color:var(--accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 25%,transparent)}
.person small{color:var(--muted)}
.avatar{flex:none;width:28px;height:28px;border-radius:50%;display:grid;place-items:center;color:#fff;font-size:11px;font-weight:700}
.avatar.lg{width:36px;height:36px;font-size:13px}
.toolbar{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:12px 0;background:var(--bg)}
.toolbar input{flex:1;min-width:220px;padding:9px 12px;border-radius:8px;border:1px solid var(--border);background:var(--panel);color:var(--text);font:inherit}
.btn{padding:8px 12px;border-radius:8px;border:1px solid var(--border);background:var(--panel);color:var(--text);font:inherit;cursor:pointer}
.btn:hover{background:var(--panel-2)}
.count{color:var(--muted);margin-left:auto}
.commit{background:var(--panel);border:1px solid var(--border);border-radius:12px;margin:10px 0;box-shadow:var(--shadow);overflow:hidden}
.commit>summary{list-style:none;display:flex;gap:12px;align-items:center;padding:12px 16px;cursor:pointer}
.commit>summary::-webkit-details-marker{display:none}
.commit[open]>summary{border-bottom:1px solid var(--border);background:var(--panel-2)}
.meta{flex:1;min-width:0}
.subject{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.byline{color:var(--muted);font-size:12.5px;display:flex;flex-wrap:wrap;gap:4px 10px;margin-top:2px}
.byline b{color:var(--text);font-weight:600}
.sha{font-family:var(--mono);font-size:12px}
.tag{font-size:11px;border:1px solid var(--border);border-radius:999px;padding:0 7px;color:var(--muted)}
.badges{display:flex;gap:6px;flex:none}
.b{font-family:var(--mono);font-size:12px;font-weight:600;padding:2px 8px;border-radius:6px;font-variant-numeric:tabular-nums}
.b.add{color:var(--add);background:var(--add-bg)}.b.del{color:var(--del);background:var(--del-bg)}.b.chg{color:var(--chg);background:var(--chg-bg)}
.b.files{color:var(--muted);background:var(--panel-2);border:1px solid var(--border)}
.body{padding:14px 16px 18px}
.msg{white-space:pre-wrap;font-family:var(--mono);font-size:12.5px;color:var(--muted);background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin:0 0 14px;max-height:180px;overflow:auto}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:12px;overflow-x:auto}
.tab{background:none;border:0;border-bottom:2px solid transparent;padding:8px 12px;font:inherit;color:var(--muted);cursor:pointer;white-space:nowrap}
.tab.on{color:var(--text);border-bottom-color:var(--accent);font-weight:600}
.tab .k{font-family:var(--mono);font-size:11.5px;margin-left:4px;padding:0 6px;border-radius:999px;background:var(--panel-2);border:1px solid var(--border)}
.file{border:1px solid var(--border);border-radius:8px;margin:10px 0;overflow:hidden}
.fhead{display:flex;gap:8px;align-items:center;padding:7px 12px;background:var(--panel-2);border-bottom:1px solid var(--border);font-family:var(--mono);font-size:12.5px}
.fhead .p{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.st{font-family:inherit;font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;padding:1px 6px;border-radius:4px;border:1px solid var(--border);color:var(--muted)}
.st.added{color:var(--add);border-color:var(--add)}.st.deleted{color:var(--del);border-color:var(--del)}.st.renamed{color:var(--chg);border-color:var(--chg)}
.code{overflow-x:auto}
table.d{border-collapse:collapse;width:100%;font-family:var(--mono);font-size:12.5px}
table.d td{padding:1px 10px;vertical-align:top;white-space:pre}
table.d td.ln{width:1%;text-align:right;color:var(--muted);user-select:none;opacity:.8}
table.d td.sg{width:1%;padding:1px 4px;user-select:none;font-weight:700}
tr.a td{background:var(--add-bg)}tr.a td.sg{color:var(--add)}
tr.r td{background:var(--del-bg)}tr.r td.sg{color:var(--del)}
tr.gap td{height:6px;background:transparent}
.split{display:grid;grid-template-columns:1fr 1fr}
.split>div{min-width:0;overflow-x:auto}.split>div+div{border-left:1px solid var(--border)}
.split .hdr{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);padding:5px 10px;border-bottom:1px solid var(--border)}
mark.o{background:var(--del-hl);color:inherit;border-radius:2px}
mark.n{background:var(--add-hl);color:inherit;border-radius:2px}
.note{color:var(--muted);font-size:12px;padding:6px 12px}
.empty{color:var(--muted);text-align:center;padding:40px 10px}
.filetbl{width:100%;border-collapse:collapse;font-size:13px}
.filetbl td{padding:6px 8px;border-bottom:1px solid var(--border)}
.filetbl td.p{font-family:var(--mono);font-size:12.5px;word-break:break-all}
.filetbl td.nums{text-align:right;white-space:nowrap}
@media (max-width:760px){
  .split{grid-template-columns:1fr}.split>div+div{border-left:0;border-top:1px solid var(--border)}
  .commit>summary{flex-wrap:wrap}.badges{width:100%;padding-left:48px}
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1 id="title"></h1>
    <div class="sub" id="subtitle"></div>
  </header>
  <section class="stats" id="stats"></section>
  <h2>Committers</h2>
  <div class="people" id="people"></div>
  <div class="toolbar">
    <input id="q" type="search" placeholder="Search message, SHA, file path or person…">
    <button class="btn" id="expand">Expand all</button>
    <button class="btn" id="collapse">Collapse all</button>
    <span class="count" id="count"></span>
  </div>
  <main id="list"></main>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtN = n => n.toLocaleString();
const fmtDate = s => new Date(s).toLocaleString(undefined, {year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
const initials = n => (n.match(/\b\w/g) || ['?']).slice(0, 2).join('').toUpperCase();
const hue = s => [...s].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
const avatar = (p, cls='') => `<span class="avatar ${cls}" style="background:hsl(${hue(p.email||p.name)} 50% 45%)" title="${esc(p.name)} <${esc(p.email)}>">${esc(initials(p.name))}</span>`;
const commitUrl = sha => D.repo_url ? `${D.repo_url}/commit/${sha}` : null;
const sum = (arr, f) => arr.reduce((a, x) => a + f(x), 0);

// ---- header & stats
document.title = `${D.repo} · commits since ${D.since.slice(0, 10)}`;
$('#title').innerHTML = D.repo_url ? `<a href="${esc(D.repo_url)}" target="_blank" rel="noopener">${esc(D.repo)}</a>` : esc(D.repo);
$('#subtitle').innerHTML = `Commits on <code>${esc(D.branch)}</code> from <b>${esc(fmtDate(D.since))}</b> to now · generated ${esc(fmtDate(D.generated))}`;
const C = D.commits;
const fileSet = new Set(C.flatMap(c => c.files.map(f => f.path)));
const people = new Map();
for (const c of C) {
  // Keyed by author: on GitHub, squash/web merges have "GitHub" as committer for everyone
  const a = c.author, key = a.email.toLowerCase() || a.name;
  const e = people.get(key) || {name: a.name, email: a.email, n: 0, add: 0, del: 0, chg: 0};
  e.n++; e.add += c.stats.added; e.del += c.stats.removed; e.chg += c.stats.changed;
  people.set(key, e);
}
$('#stats').innerHTML = [
  ['Commits', C.length, ''], ['People', people.size, ''], ['Files touched', fileSet.size, ''],
  ['Lines added', sum(C, c => c.stats.added), 'add'], ['Lines removed', sum(C, c => c.stats.removed), 'del'],
  ['Lines changed', sum(C, c => c.stats.changed), 'chg'],
].map(([l, n, k]) => `<div class="stat ${k}"><div class="n">${fmtN(n)}</div><div class="l">${l}</div></div>`).join('');

// ---- people filter
let personFilter = null;
const sorted = [...people.entries()].sort((a, b) => b[1].n - a[1].n);
$('#people').innerHTML = sorted.length ? sorted.map(([k, p]) =>
  `<button class="person" data-k="${esc(k)}">${avatar(p)}<span>${esc(p.name)} <small>${p.n} commit${p.n > 1 ? 's' : ''} · <span style="color:var(--add)">+${fmtN(p.add)}</span> <span style="color:var(--chg)">~${fmtN(p.chg)}</span> <span style="color:var(--del)">−${fmtN(p.del)}</span></small></span></button>`
).join('') : '<span class="note">None</span>';
$('#people').addEventListener('click', e => {
  const b = e.target.closest('.person'); if (!b) return;
  personFilter = personFilter === b.dataset.k ? null : b.dataset.k;
  document.querySelectorAll('.person').forEach(x => x.classList.toggle('on', x.dataset.k === personFilter));
  applyFilter();
});

// ---- commit list
const byline = c => {
  const a = c.author, m = c.committer;
  const sameP = a.email.toLowerCase() === m.email.toLowerCase() && a.name === m.name;
  const url = commitUrl(c.sha);
  return `<span><b>${esc(a.name)}</b>${sameP ? ' committed' : ' authored'}</span>` +
    (sameP ? '' : `<span>committed by <b>${esc(m.name)}</b></span>`) +
    `<span>${esc(fmtDate(m.date))}</span>` +
    `<span class="sha">${url ? `<a href="${url}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${c.sha.slice(0, 7)}</a>` : c.sha.slice(0, 7)}</span>` +
    (c.merge ? '<span class="tag">merge</span>' : '');
};
$('#list').innerHTML = C.length ? C.map((c, i) => `
<details class="commit" data-i="${i}">
  <summary>
    ${avatar(c.author, 'lg')}
    <div class="meta"><div class="subject" title="${esc(c.subject)}">${esc(c.subject)}</div><div class="byline">${byline(c)}</div></div>
    <div class="badges">
      <span class="b add" title="lines added">+${fmtN(c.stats.added)}</span>
      <span class="b chg" title="lines changed">~${fmtN(c.stats.changed)}</span>
      <span class="b del" title="lines removed">−${fmtN(c.stats.removed)}</span>
      <span class="b files">${c.files.length} file${c.files.length === 1 ? '' : 's'}</span>
    </div>
  </summary>
  <div class="body"></div>
</details>`).join('') : `<div class="empty">No commits on <b>${esc(D.branch)}</b> since ${esc(fmtDate(D.since))}.</div>`;

$('#list').addEventListener('toggle', e => {
  const d = e.target; if (!d.open) return;
  const body = d.querySelector('.body');
  if (!body.dataset.done) { body.dataset.done = 1; renderBody(body, C[d.dataset.i]); }
}, true);

// ---- commit details
const TABS = [['changed', 'Changed'], ['added', 'Added'], ['removed', 'Removed'], ['files', 'Files']];
function renderBody(el, c) {
  const firstTab = TABS.find(([k]) => c.stats[k] > 0)?.[0] || 'files';
  el.innerHTML = (c.body ? `<pre class="msg">${esc(c.body)}</pre>` : '') +
    `<div class="tabs">${TABS.map(([k, l]) => `<button class="tab${k === firstTab ? ' on' : ''}" data-t="${k}">${l}<span class="k">${k === 'files' ? c.files.length : fmtN(c.stats[k])}</span></button>`).join('')}</div><div class="pane"></div>`;
  const pane = el.querySelector('.pane');
  const show = t => { pane.innerHTML = renderPane(c, t); el.querySelectorAll('.tab').forEach(b => b.classList.toggle('on', b.dataset.t === t)); };
  el.querySelector('.tabs').addEventListener('click', e => { const b = e.target.closest('.tab'); if (b) show(b.dataset.t); });
  show(firstTab);
}

const segs = (parts, cls) => parts.map(([h, t]) => h ? `<mark class="${cls}">${esc(t)}</mark>` : esc(t)).join('');
const fileHead = (f, extra='') => `<div class="fhead"><span class="st ${f.status}">${f.status}</span><span class="p" title="${esc(f.path)}">${
  f.status === 'renamed' ? `${esc(f.old_path)} → ` : ''}${esc(f.path)}</span>${extra}</div>`;
const truncNote = (f, k) => f.counts[k] > f[k].length ? `<div class="note">Showing first ${f[k].length} of ${fmtN(f.counts[k])} lines.</div>` : '';

function renderPane(c, t) {
  if (t === 'files') {
    if (!c.files.length) return `<div class="empty">${c.merge ? 'Merge commit — no direct changes shown.' : 'No file changes.'}</div>`;
    return `<table class="filetbl">${c.files.map(f => `<tr><td style="width:1%"><span class="st ${f.status}">${f.status}</span></td>
      <td class="p">${f.status === 'renamed' ? esc(f.old_path) + ' → ' : ''}${esc(f.path)}${f.binary ? ' <span class="tag">binary</span>' : ''}</td>
      <td class="nums"><span class="b add">+${f.counts.added}</span> <span class="b chg">~${f.counts.changed}</span> <span class="b del">−${f.counts.removed}</span></td></tr>`).join('')}</table>`;
  }
  const files = c.files.filter(f => f.counts[t] > 0);
  if (!files.length) return `<div class="empty">No ${t} lines in this commit.</div>`;
  return files.map(f => {
    if (t === 'changed') {
      const rowsO = [], rowsN = [];
      f.changed.forEach((x, i) => {
        if (i && f.changed[i - 1].new_ln !== x.new_ln - 1) { rowsO.push('<tr class="gap"><td colspan="3"></td></tr>'); rowsN.push('<tr class="gap"><td colspan="3"></td></tr>'); }
        rowsO.push(`<tr class="r"><td class="ln">${x.old_ln}</td><td class="sg">−</td><td>${segs(x.old, 'o')}</td></tr>`);
        rowsN.push(`<tr class="a"><td class="ln">${x.new_ln}</td><td class="sg">+</td><td>${segs(x.new, 'n')}</td></tr>`);
      });
      return `<div class="file">${fileHead(f, `<span class="b chg">~${f.counts.changed}</span>`)}
        <div class="split"><div><div class="hdr">Before</div><table class="d">${rowsO.join('')}</table></div>
        <div><div class="hdr">After</div><table class="d">${rowsN.join('')}</table></div></div>${truncNote(f, t)}</div>`;
    }
    const cls = t === 'added' ? 'a' : 'r', sign = t === 'added' ? '+' : '−';
    const rows = f[t].map(([ln, text], i) =>
      (i && f[t][i - 1][0] !== ln - 1 ? '<tr class="gap"><td colspan="3"></td></tr>' : '') +
      `<tr class="${cls}"><td class="ln">${ln}</td><td class="sg">${sign}</td><td>${esc(text)}</td></tr>`).join('');
    return `<div class="file">${fileHead(f, `<span class="b ${t === 'added' ? 'add' : 'del'}">${sign}${f.counts[t]}</span>`)}
      <div class="code"><table class="d">${rows}</table></div>${truncNote(f, t)}</div>`;
  }).join('');
}

// ---- search / filter
const hay = C.map(c => [c.sha, c.subject, c.body, c.author.name, c.author.email, c.committer.name, c.committer.email,
  ...c.files.flatMap(f => [f.path, f.old_path])].join('\n').toLowerCase());
function applyFilter() {
  const q = $('#q').value.trim().toLowerCase();
  let shown = 0;
  document.querySelectorAll('.commit').forEach(d => {
    const c = C[d.dataset.i];
    const ok = (!q || hay[d.dataset.i].includes(q)) && (!personFilter || (c.author.email.toLowerCase() || c.author.name) === personFilter);
    d.hidden = !ok; shown += ok;
  });
  $('#count').textContent = `${shown} of ${C.length} commits`;
}
$('#q').addEventListener('input', applyFilter);
$('#expand').onclick = () => document.querySelectorAll('.commit:not([hidden])').forEach(d => d.open = true);
$('#collapse').onclick = () => document.querySelectorAll('.commit').forEach(d => d.open = false);
applyFilter();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
