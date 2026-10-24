"""Round 4: write git fast-import streams for the local test repos. usage: mkrepos.py <out-dir>
  small.fi : 5 commits on main (used for declared.git and undeclared.git)
  big.fi   : 30000 linear commits on main, each also the tip of its own branch refs/heads/b00000.. (30000 distinct
             wants on a full clone: enough to push the request body past http.postBuffer's 1 MiB default)
  more.fi  : 50 new commits on main + 20 new branches, applied to big.git AFTER the clone (the fetch case)
"""
import sys
from pathlib import Path

out = Path(sys.argv[1])


def stream(n, ref_each=False, start=0, parent_ref=None, newbranches=0):
    s = []
    t = 1792800000
    for i in range(start, start + n):
        s.append("commit refs/heads/main\n")
        s.append(f"mark :{i + 1}\n")
        s.append(f"committer Spike <spike@rb.test> {t + i} +0000\n")
        msg = f"c{i}\n"
        s.append(f"data {len(msg)}\n{msg}")
        if i == start and parent_ref:
            s.append(f"from {parent_ref}^0\n")
        content = f"line {i}\n" * 3
        s.append(f"M 644 inline f{i % 200}.txt\ndata {len(content)}\n{content}\n")
        if ref_each:
            s.append(f"reset refs/heads/b{i:05d}\nfrom :{i + 1}\n\n")
    for j in range(newbranches):
        s.append(f"reset refs/heads/new{j:03d}\nfrom :{start + n - j}\n\n")
    return "".join(s)


def client_only(branches=300, per=10):
    """client.fi: 300 branches x 10 commits of history the origin never saw (CLIENT side), so fetch haves go unanswered."""
    s, mark = [], 100000
    for b in range(branches):
        for k in range(per):
            mark += 1
            s.append(f"commit refs/heads/loc{b:03d}\nmark :{mark}\ncommitter Spike <spike@rb.test> {1792900000 + mark} +0000\n")
            msg = f"local {b} {k}\n"
            s.append(f"data {len(msg)}\n{msg}")
            content = f"local {b} {k}\n"
            s.append(f"M 644 inline loc{b}.txt\ndata {len(content)}\n{content}\n")
    return "".join(s)


def mid():
    """mid.fi: 8000 commits each on its own branch (a full clone's want list ~400 KB: above 1 KiB, under postBuffer)."""
    return stream(8000, ref_each=True)


(out / "client.fi").write_text(client_only())
(out / "mid.fi").write_text(mid())
(out / "small.fi").write_text(stream(5))
(out / "big.fi").write_text(stream(30000, ref_each=True))
(out / "more.fi").write_text(stream(50, start=30000, parent_ref="refs/heads/main", newbranches=20))
print("streams written", out)
