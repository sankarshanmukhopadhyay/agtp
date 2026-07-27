#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,os,pathlib,re,subprocess,sys
ROOT=pathlib.Path(__file__).resolve().parents[1]; CFG=ROOT/'.github/upstream-tracking.yml'; CP=ROOT/'.upstream/checkpoint.json'
def cfg(key,default=''):
 t=CFG.read_text(); m=re.search(rf'^\s*{re.escape(key)}:\s*["\']?([^"\'\n#]+)',t,re.M); return m.group(1).strip() if m else default
def run(*a,check=True):
 p=subprocess.run(a,cwd=ROOT,text=True,capture_output=True)
 if check and p.returncode: raise RuntimeError(p.stderr.strip() or 'command failed')
 return p.stdout.strip()
def out(k,v):
 if os.getenv('GITHUB_OUTPUT'):
  with open(os.environ['GITHUB_OUTPUT'],'a') as f:f.write(f'{k}={v}\n')
 print(f'{k}={v}')
def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--write-report',default='.upstream/drift-report.md'); n=ap.parse_args()
 enabled=cfg('enabled','false').lower()=='true'; upstream=cfg('repository'); branch=cfg('branch','main')
 if not upstream or '/' not in upstream: raise SystemExit('Invalid upstream declaration')
 if os.getenv('GITHUB_REPOSITORY','').lower()==upstream.lower(): raise SystemExit('Upstream equals executing repository')
 if not enabled: out('enabled','false'); out('drift','false'); return
 run('git','fetch','--no-tags','--prune',f'https://github.com/{upstream}.git',f'+refs/heads/{branch}:refs/remotes/upstream/{branch}')
 head=run('git','rev-parse',f'refs/remotes/upstream/{branch}'); base=run('git','merge-base','HEAD',f'refs/remotes/upstream/{branch}',check=False)
 behind=run('git','rev-list','--count',f'HEAD..refs/remotes/upstream/{branch}'); ahead=run('git','rev-list','--count',f'refs/remotes/upstream/{branch}..HEAD')
 files=run('git','diff','--name-only',f'HEAD...refs/remotes/upstream/{branch}',check=False).splitlines(); drift=int(behind)>0
 d=json.loads(CP.read_text()); d['lastObservedCommit']=head; CP.write_text(json.dumps(d,indent=2)+'\n')
 report=ROOT/n.write_report; report.parent.mkdir(parents=True,exist_ok=True)
 report.write_text(f'# Upstream drift report\n\n- Upstream: `{upstream}`\n- Branch: `{branch}`\n- Upstream head: `{head}`\n- Merge base: `{base or "unavailable"}`\n- Fork commits ahead: {ahead}\n- Upstream commits not integrated: {behind}\n- Drift detected: `{str(drift).lower()}`\n\n## Changed paths\n\n'+('\n'.join(f'- `{x}`' for x in files) if files else '- None')+'\n')
 for k,v in [('enabled','true'),('drift',str(drift).lower()),('upstream',upstream),('branch',branch),('head',head),('behind',behind),('ahead',ahead)]: out(k,v)
if __name__=='__main__':
 try: main()
 except Exception as e: print(f'error: {e}',file=sys.stderr); raise SystemExit(1)
