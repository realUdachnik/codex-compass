"""One native Codex invocation with scoped, bounded observational reminders."""
from __future__ import annotations
import hashlib,json,shlex,sys,uuid
from datetime import datetime,timezone
from pathlib import Path
from .evidence import atomic_json
from .process import run_process


def complete_usage(usage):
 if not isinstance(usage,dict):return False
 if not all(type(usage.get(k)) is int and 0<=usage[k]<=2**63-1 for k in ['input_tokens','cached_input_tokens','output_tokens']):return False
 return usage['cached_input_tokens']<=usage['input_tokens']


def request_usage_audit(directory,terminal_usage):
 """Reconcile counters without treating diagnostic sums as another final bill.

 CLI0.171.0's terminal aggregate excludes a separately observed compaction call.
 A mismatch or observed compaction therefore prevents a completeness claim.
 """
 records=[];valid=True
 for path in sorted(Path(directory).glob('*.jsonl')):
  try:
   for line in path.read_text().splitlines():
    value=json.loads(line)
    if not isinstance(value,dict):raise ValueError('Invalid observation record')
    records.append(value)
  except (OSError,ValueError):valid=False
 requests=[r for r in records if r.get('event')=='PostModelRequest'];counts={}
 for record in requests:
  values=record.get('token_usage')
  if not isinstance(values,dict):continue
  for key,value in values.items():
   if type(value) is int and value>=0:counts[key]=counts.get(key,0)+value
 usable=sum(complete_usage(r.get('token_usage')) for r in requests)
 matched=complete_usage(terminal_usage) and all(counts.get(k)==terminal_usage[k] for k in ['input_tokens','cached_input_tokens','output_tokens'])
 write_values=[(r.get('token_usage') or {}).get('cache_write_input_tokens') if isinstance(r.get('token_usage'),dict) else None for r in requests]
 terminal_writes=terminal_usage.get('cache_write_input_tokens') if isinstance(terminal_usage,dict) else None
 writes_match=bool(requests and type(terminal_writes) is int and terminal_writes>=0 and all(type(v) is int and v>=0 for v in write_values) and sum(write_values)==terminal_writes)
 compactions=sum(r.get('event')=='SessionStart' and r.get('source')=='compact' for r in records)
 complete=bool(valid and requests and usable==len(requests) and matched and writes_match and not compactions)
 reason=None if complete else ('Отдельный вызов сокращения контекста может отсутствовать в итоговом счётчике.' if compactions else 'Итоговый расход не подтверждён полным согласованным журналом запросов.')
 return {'observed_requests':len(requests),'requests_with_usable_usage':usable,'known_request_sum_diagnostic_only':counts or None,
         'terminal_matches_known_sum':bool(matched),'cache_write_counter_matches':writes_match,'observed_compactions':compactions,'captured_logs_valid':valid,
         'complete_for_standard_estimate':complete,'reason_ru':reason,'diagnostic_sum_is_not_added_to_terminal_usage':True}


def skill_directory():
 # Shipped runtime lives under SKILL/runtime/codex_compass; source under REPO/.
 package=Path(__file__).resolve().parent
 if (package.parents[1]/'SKILL.md').is_file():return package.parents[1]
 source=package.parent/'skills/codex-compass'
 if (source/'SKILL.md').is_file():return source
 raise ValueError('Cannot locate the supplied skill')


def hook_definitions(workspace,state_dir,mode,*,python=None):
 if mode not in ['observe','adaptive']:raise ValueError('Unknown reminder mode')
 script=skill_directory()/'scripts/watch.py'
 command=shlex.join([str(python or sys.executable),'-I',str(script),'--workspace',str(Path(workspace).resolve()),
                      '--state-dir',str(Path(state_dir).resolve()),'--mode',mode])
 def group(matcher=None):
  handler={'type':'command','command':command,'timeout':5,'additionalContextLimit':200}
  return [dict({'hooks':[handler]},**({'matcher':matcher} if matcher else {}))]
 return {'SessionStart':group(), 'PostToolUse':group(), 'PostModelRequest':[{'hooks':[{'type':'command','command':command,'timeout':5}]}]}


def run_assisted(workspace,goal,*,model=None,reasoning=None,seconds=1800,run_dir=None):
 from .hook_config import prepare_hook_config
 workspace=Path(workspace).expanduser().resolve(strict=True)
 if not isinstance(goal,str) or not goal.strip():raise ValueError('A concrete task is required')
 if not 1<=seconds<=14400:raise ValueError('Time limit must be between1 and14400 seconds')
 directory=Path(run_dir).expanduser().resolve() if run_dir else workspace/'.compass'/'assisted'/uuid.uuid4().hex
 if directory.is_relative_to(workspace) and not directory.is_relative_to(workspace/'.compass'):raise ValueError('In-project run directories must be inside .compass')
 directory.mkdir(parents=True,exist_ok=False);directory.chmod(0o700)
 definitions=hook_definitions(workspace,directory/'observations','adaptive')
 prepared=prepare_hook_config(definitions,workspace,reject_foreign=True)
 atomic_json(directory/'hook-proof.json',prepared['metadata'])
 skill=(skill_directory()/'SKILL.md').read_text()
 developer=('Use the supplied Compass skill for the user\'s task, within existing authorization and permissions. '
            'Do not recursively launch Compass assist. Native reminders are advisory observations; they do not change the task or prove correctness.\n\n'+skill)
 command=['codex','exec','--json','--ephemeral','--skip-git-repo-check','--cd',str(workspace),
          '-c','developer_instructions='+json.dumps(developer),
          '-c','skills.config=[{name="codex-compass",enabled=false}]',*prepared['config_flags']]
 if model:command+=['-m',model]
 if reasoning:command+=['-c','model_reasoning_effort='+json.dumps(reasoning)]
 command+=['--output-last-message',str(directory/'answer.txt'),'-']
 atomic_json(directory/'invocation.json',{'started_at_utc':datetime.now(timezone.utc).isoformat(),
           'goal_sha256':hashlib.sha256(goal.encode()).hexdigest(),'model':model,'reasoning':reasoning,
           'seconds':seconds,'global_configuration_changed':False,'single_native_invocation':True})
 process=run_process(command,workspace,seconds,8000000,goal);atomic_json(directory/'process.json',process)
 answer=(directory/'answer.txt').read_text() if (directory/'answer.txt').exists() else None
 usage={};complete=False
 for line in process['stdout'].splitlines():
  try:event=json.loads(line)
  except ValueError:continue
  if event.get('type')=='turn.completed':
   complete=True
   for key,value in event.get('usage',{}).items():
    if type(value) is int and value>=0:usage[key]=usage.get(key,0)+value
 status='completed' if complete and process['returncode']==0 and not process['timed_out'] else 'time_limit' if process['timed_out'] else 'not_completed'
 audit=request_usage_audit(directory/'observations',usage)
 result={'status':status,'run_dir':str(directory),'final_answer':answer,'usage':usage or None,
         'usage_complete':complete_usage(usage) and complete and not process['output_truncated'] and audit['complete_for_standard_estimate'],
         'request_usage_audit':audit,
         'seconds':process['duration_seconds'],'quality_evaluation':'Native turn completion, not independent acceptance grading','reminder_mode':'adaptive','new_turns_started_by_reminder':0}
 atomic_json(directory/'report.json',result);return result
