#!/usr/bin/env python3
from pathlib import Path
import re,sys
R=Path(__file__).resolve().parents[1]; bad=[]
patterns=[
 ("push to upstream remote",r"git\s+push\s+upstream(?:\s|$)"),
 ("GitHub operation targeting upstream variable",r"--repo[ =]+[\"\']?\$\{?UPSTREAM(?:_REPOSITORY)?\}?"),
 ("GitHub operation targeting upstream remote",r"--repo[ =]+[\"\']?upstream(?:[\"\']|\s|$)"),
]
for p in (R/".github/workflows").glob("*.yml"):
 t=p.read_text()
 for label,pat in patterns:
  if re.search(pat,t,re.I): bad.append(f"{p.relative_to(R)}: {label}")
if bad: print("\n".join(bad),file=sys.stderr); sys.exit(1)
print("Upstream directionality policy validated.")
