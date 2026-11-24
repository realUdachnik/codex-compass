"""Bounded local reminders from observable work, never a claim to read memory.

Native lifecycle hooks supply data. This module never calls a model, blocks a
command, approves an action, or starts another turn. All arms can observe the
same events; only adaptive mode returns short developer context.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import ast,hashlib,json,os,re,shlex,stat,subprocess,sys,time
from pathlib import Path
from .evidence import atomic_json
from .process import run_process

VERSION=1
MAX_REMINDERS=4
COOLDOWN=6
UNCHECKED_TOOLS=10
CHECKPOINT_TOOLS=24
MESSAGES={
 'changed_after_check':'Compass: working files changed after the last observed check. Once this edit is complete, check the new behavior and an important existing case against the current files. Earlier checks do not verify later edits.',
 'unchecked_changes':'Compass checkpoint: several work steps followed changes without an observed behavior check. Resolve the important uncertainty with a focused check, then fix concrete high-impact failures. Do not add speculative scope.',
 'repeated_failure':'Compass: the same command failed again with unchanged observed working files. Reconsider the cause or try a focused alternative instead of repeating it unchanged. Do not weaken the requirement to make a check pass.',
 'repeated_action':'Compass: the same command has been repeated with unchanged observed working files. Check whether another repetition adds useful evidence; if it does not, reconsider the approach. This observation alone does not mean the command failed.',
 'compacted':'Compass after context compaction: keep the original outcome and constraints, inspect actual related code, prefer a simple fitting approach, anticipate consequential failures, and verify current behavior plus an important old case. Fix concrete failures, then stop polishing.',
 'checkpoint':'Compass checkpoint: compare the current work with the original outcome and constraints. Spend the remaining effort on the most important unverified behavior or concrete failure, then finish with an accurate brief result. More changes alone are not progress.',
}
SUFFIXES={'.py','.js','.mjs','.jsx','.ts','.tsx','.json','.toml','.yaml','.yml','.md','.txt','.sql','.html','.css','.go','.rs','.java','.c','.h','.sh'}
EXCLUDE={'.git','.compass','.venv','venv','node_modules','__pycache__','.pytest_cache','.mypy_cache','.ruff_cache','private'}

def file_observation(workspace,relative,limit):
 """Read only via no-follow directory descriptors; a changed parent is unknown."""
 opened=[os.open(workspace,os.O_RDONLY|os.O_DIRECTORY)]
 try:
  fd=opened[0]
  for part in relative.parts[:-1]:
   fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);opened.append(fd)
  try:info=os.stat(relative.name,dir_fd=fd,follow_symlinks=False)
  except FileNotFoundError:return b'deleted',True,0
  if stat.S_ISLNK(info.st_mode):return b'link:'+os.fsencode(os.readlink(relative.name,dir_fd=fd)),True,0
  if not stat.S_ISREG(info.st_mode) or info.st_size>limit:return b'',False,0
  content_fd=os.open(relative.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd);opened.append(content_fd)
  before=os.fstat(content_fd);chunks=[];total=0
  while True:
   chunk=os.read(content_fd,min(65536,limit-total+1))
   if not chunk:break
   total+=len(chunk)
   if total>limit:return b'',False,0
   chunks.append(chunk)
  after=os.fstat(content_fd)
  stable=(before.st_size,before.st_mtime_ns,before.st_ino)==(after.st_size,after.st_mtime_ns,after.st_ino)
  return hashlib.sha256(b''.join(chunks)).digest(),stable,total
 finally:
  for fd in reversed(opened):os.close(fd)


def signature(workspace:Path):
 """Hash relevant git changes, including contents, with explicit bounded coverage."""
 try:
  p=run_process(['git','status','--porcelain=v1','-z','--untracked-files=all'],workspace,2,131072)
  if p['returncode'] or p['timed_out'] or p['output_truncated'] or '\ufffd' in p['stdout']:return {'value':None,'complete':False,'files':None}
  chunks=p['stdout'].encode().split(b'\0');paths=[];i=0
  while i<len(chunks):
   row=chunks[i];i+=1
   if len(row)<4:continue
   code=row[:2].decode('ascii',errors='replace');name=os.fsdecode(row[3:])
   paths.append(name)
   if 'R' in code or 'C' in code:i+=1
  h=hashlib.sha256();count=0;remaining=20000000;complete=True;deadline=time.monotonic()+.75
  for name in sorted(set(paths)):
   if count>=1000 or time.monotonic()>deadline:complete=False;break
   rel=Path(name)
   if rel.is_absolute() or '..' in rel.parts or any(x in EXCLUDE or x.startswith('.env') for x in rel.parts):continue
   if rel.suffix.lower() not in SUFFIXES:continue
   path=workspace/rel;count+=1;h.update(os.fsencode(name));h.update(b'\0')
   try:observed,safe,size=file_observation(workspace,rel,min(4000000,remaining))
   except OSError:observed,safe,size=b'',False,0
   h.update(observed);remaining-=size
   if not safe:complete=False
  return {'value':h.hexdigest(),'complete':complete,'files':count}
 except (OSError,subprocess.SubprocessError):return {'value':None,'complete':False,'files':None}


def check_command(command):
 """Recognize invocations, not mentions of test names in echo/search output."""
 def has_assert(code):
  try:return any(isinstance(node,ast.Assert) for node in ast.walk(ast.parse(code)))
  except (SyntaxError,ValueError):return False
 # Python here-doc probes are parsed as Python, never as arbitrary shell words.
 first=command.splitlines()[0] if command else ''
 if re.search(r"\bpython[0-9.]*\s+(?:-[BIS]\s+)*-\s*<<",first):
  try:
   header=shlex.split(first)
   while header and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*=.*',header[0]):header.pop(0)
   if header and re.fullmatch(r'python[0-9.]*',Path(header[0]).name):
    lines=command.splitlines();marker=first.split('<<',1)[1].strip().strip("\"'")
    body=[]
    for line in lines[1:]:
     if line.strip()==marker:break
     body.append(line)
    if has_assert('\n'.join(body)):return True
  except ValueError:pass
 try:
  lexer=shlex.shlex(command,posix=True,punctuation_chars=';&|');lexer.whitespace_split=True;tokens=list(lexer)
 except ValueError:return False
 segments=[[]]
 for token in tokens:
  if token in [';','&&','||','|','&']:segments.append([])
  else:segments[-1].append(token)
 for args in segments:
  while args and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*=.*',args[0]):args=args[1:]
  if args and Path(args[0]).name in ['uv','poetry'] and len(args)>2 and args[1]=='run':args=args[2:]
  if not args:continue
  exe=Path(args[0]).name;tail=args[1:]
  if exe=='pytest' and not any(x in tail for x in ['--collect-only','--help','--version']):return True
  if re.fullmatch(r'python[0-9.]*',exe):
   if '-m' in tail:
    i=tail.index('-m');mod=tail[i+1] if i+1<len(tail) else ''
    if mod in ['pytest','unittest'] and not any(x in tail for x in ['--collect-only','--help','--version']):return True
   if '-c' in tail:
    i=tail.index('-c')
    if i+1<len(tail) and has_assert(tail[i+1]):return True
  if exe in ['npm','pnpm','cargo','go'] and tail and tail[0]=='test':return True
 return False


def command_info(payload):
 """Conservative evidence classification. Unknown success stays unknown."""
 inp=payload.get('tool_input') or {};command=inp.get('command',inp.get('cmd','')) if isinstance(inp,dict) else ''
 if not isinstance(command,str):command=''
 response=payload.get('tool_response');code=None
 if isinstance(response,dict):
  for key in ['exit_code','returncode','exitCode']:
   if type(response.get(key)) is int:code=response[key];break
  output=response.get('output',response.get('stdout',response.get('aggregated_output','')))
 else:output=response if isinstance(response,str) else ''
 if not isinstance(output,str):output=''
 test_command=check_command(command)
 inline_probe=False
 observed_success=code==0 and (test_command or inline_probe)
 if re.search(r'\b(?:no tests ran|collected 0 items|Ran 0 tests)\b',output,re.I):observed_success=False
 return {'command_sha256':hashlib.sha256(command.encode()).hexdigest() if command else None,
         'exit_code':code,'successful_check_observed':observed_success,'check_attempt_observed':test_command or inline_probe}


def fresh_state(sig,now):
 return {'version':VERSION,'initial_signature':sig,'signature':sig,'tool_steps':0,'since_check':0,
         'last_check_signature':None,'check_observed':False,'last_reminder_step':-COOLDOWN,
         'reminders':[],'seen_ids':[],'failure_counts':{},'action_counts':{},'edit_series_reminded':False,
         'started_at':now,'last_turn':None,'checkpoint_sent':False,'compactions':0}


def observe(state,payload,sig,mode,now=None):
 """Pure transition returns state, minimal audit data and optional fixed context."""
 now=time.time() if now is None else now
 event=payload.get('hook_event_name');record={'event':event,'time':now,'source':payload.get('source'),
   'tool_name':payload.get('tool_name'),'signature':sig,'signal':None,'reminder':None}
 turn=payload.get('turn_id')
 if turn:state['last_turn']=turn
 reason=None
 if event=='PostModelRequest':
  usage=payload.get('token_usage')
  record['request_status']=payload.get('status')
  record['token_usage']={k:v for k,v in usage.items() if type(v) is int and v>=0} if isinstance(usage,dict) else None
  return state,record,None
 if event=='SessionStart' and payload.get('source')=='compact':
  state['compactions']+=1;reason='compacted'
 elif event=='PostToolUse':
  uid=payload.get('tool_use_id');seen=state['seen_ids']
  if uid and uid in seen:record['duplicate']=True;return state,record,None
  if uid:state['seen_ids']=(seen+[uid])[-512:]
  state['tool_steps']+=1;state['since_check']+=1
  info=command_info(payload);record.update(info)
  old=state['signature'];changed=sig.get('complete') and old.get('complete') and sig['value']!=old['value']
  state['signature']=sig
  checked=info['check_attempt_observed'] and sig.get('complete')
  if checked:
   state.update(last_check_signature=sig['value'],check_observed=True,since_check=0,edit_series_reminded=False)
  failed=info['exit_code'] is not None and info['exit_code']!=0 and info['command_sha256']
  key=info['command_sha256']+':'+sig['value'] if info['command_sha256'] and sig.get('complete') else None
  if changed:state['failure_counts']={};state['action_counts']={}
  if key:
   actions=state['action_counts'];actions[key]=actions.get(key,0)+1;state['action_counts']=dict(list(actions.items())[-32:])
   if failed:
    counts=state['failure_counts'];counts[key]=counts.get(key,0)+1;state['failure_counts']=dict(list(counts.items())[-32:])
    if counts[key]>=2:reason='repeated_failure'
   elif info['exit_code']==0:state['failure_counts'].pop(key,None)
   elif actions[key]>=3:reason='repeated_action'
  if not reason and not checked and changed and state['check_observed'] and sig['value']!=state['last_check_signature'] and not state['edit_series_reminded']:
   reason='changed_after_check'
  dirty=sig.get('complete') and sig['value']!=state['initial_signature'].get('value')
  if not reason and dirty and state['since_check']>=UNCHECKED_TOOLS:reason='unchecked_changes'
  if not reason and not state['checkpoint_sent'] and state['tool_steps']>=CHECKPOINT_TOOLS:reason='checkpoint'
 record['signal']=reason
 context=None
 if reason:
  # Reserve the fourth slot for post-compaction restoration, rather than periodic chatter.
  cap=MAX_REMINDERS if reason=='compacted' else MAX_REMINDERS-1
  cooled=reason=='compacted' or state['tool_steps']-state['last_reminder_step']>=COOLDOWN
  if mode=='adaptive' and len(state['reminders'])<cap and cooled:
   context=MESSAGES[reason]
   state['reminders'].append({'reason':reason,'step':state['tool_steps'],'time':now,'chars':len(context)})
   state['last_reminder_step']=state['tool_steps'];record['reminder']=reason
   if reason=='changed_after_check':state['edit_series_reminded']=True
   if reason=='checkpoint':state['checkpoint_sent']=True
 return state,record,context


@contextmanager
def locked(path):
 import fcntl
 with path.open('a+') as f:
  os.chmod(path,0o600);fcntl.flock(f.fileno(),fcntl.LOCK_EX)
  try:yield
  finally:fcntl.flock(f.fileno(),fcntl.LOCK_UN)


def handle(payload,workspace,state_dir,mode):
 if mode not in ['observe','adaptive']:raise ValueError('Unknown reminder mode')
 workspace=Path(workspace).resolve();state_dir=Path(state_dir).resolve()
 if state_dir.is_relative_to(workspace) and not state_dir.is_relative_to(workspace/'.compass'):raise ValueError('In-workspace observation state must be inside .compass')
 state_dir.mkdir(parents=True,exist_ok=True)
 if Path(payload.get('cwd','')).resolve()!=workspace:raise ValueError('Hook workspace differs from configured workspace')
 session=payload.get('session_id')
 if not isinstance(session,str) or not session:raise ValueError('Missing session id')
 key=hashlib.sha256(session.encode()).hexdigest()[:32];path=state_dir/(key+'.json');log=state_dir/(key+'.jsonl')
 with locked(state_dir/(key+'.lock')):
  # Usage-only events do not need a filesystem scan.
  existing=json.loads(path.read_text()) if path.exists() else None
  sig=existing['signature'] if existing and payload.get('hook_event_name')=='PostModelRequest' else signature(workspace)
  state=existing or fresh_state(sig,time.time())
  state,record,context=observe(state,payload,sig,mode)
  atomic_json(path,state)
  with log.open('a',encoding='utf-8') as f:os.chmod(log,0o600);f.write(json.dumps(record,ensure_ascii=False)+'\n')
 if context:return {'hookSpecificOutput':{'hookEventName':payload['hook_event_name'],'additionalContext':context}}
 return {}


def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workspace',type=Path,required=True);p.add_argument('--state-dir',type=Path,required=True);p.add_argument('--mode',choices=['observe','adaptive'],required=True);a=p.parse_args(argv)
 try:
  raw=sys.stdin.buffer.read(16000001)
  if len(raw)>16000000:raise ValueError('Hook input exceeds local bound')
  value=json.loads(raw);result=handle(value,a.workspace,a.state_dir,a.mode)
  print(json.dumps(result,ensure_ascii=False));return 0
 except (OSError,ValueError,KeyError,TypeError) as e:
  # Do not echo input, tool output, source text, commands or credentials.
  print('Compass reminder observation unavailable: '+type(e).__name__,file=sys.stderr);return 1

if __name__=='__main__':raise SystemExit(main())
