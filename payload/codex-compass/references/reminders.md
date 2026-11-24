# Bounded native reminders

Use the `assist` launcher when the user requests externally monitored reminders
for a long task. It starts **one** native Codex invocation with the complete
skill and scoped lifecycle hooks. Do not launch it recursively or inside an
invocation whose policy forbids additional model calls. Ordinary small work
needs no launcher.

```sh
python <skill>/scripts/compass.py assist --workspace <project> --goal 'Concrete task'
```

`--model` and `--reasoning` are optional; omission uses existing **CLI** settings,
not a guess about another chat's active model. `--seconds` defaults to 1800.
The launcher uses the standalone CLI permission settings. It cannot infer a
stricter profile selected only in another chat. Existing parent restrictions
still apply: do not use another invocation to bypass them, and pass task
constraints explicitly. The comparison harness supplies its identical local
profile explicitly for every mode.
No global configuration or trust entry is installed.

The launcher checks native hook metadata and temporarily trusts only its exact
hook definitions for this invocation. It currently fails explicitly if foreign
hooks are present, instead of replacing their definitions or trust. Native
interfaces were tested with CLI 0.171.0; other versions must pass the same
capability preflight. Definition hashes do not authenticate local script bytes.

A deterministic observer records code-file fingerprints and supported tool
outcomes. Native Bash hooks may expose stdout without an exit code: unknown
codes stay unknown. Three identical actions then produce only a neutral repetition
checkpoint, never an assertion that they failed. Recognized test commands indicate
a check attempt, not success. It can remind after repeated identical failures without observed code
changes, later changes after an observed check attempt, a long unverified
change sequence, or a sparse work checkpoint. After context compaction,
SessionStart restores a short workflow anchor. These are signals, not a test of
what the model remembers, and a command classified as a check is not proof of
correctness. Binary assets and ignored/generated files are outside the code
fingerprint. Arbitrary tools and custom checks may not be recognized.

At most 4 reminders are sent per native session; ordinary signals can use 3, with one slot
reserved for compaction. A 6-tool cooldown limits ordinary reminders. Text is
fixed, short, and never interpolates tool output or repository content. The
observer does not block commands, approve permissions, change the user's goal,
launch models, or create follow-up turns. When the cap is exhausted, further
compactions may receive no reminder; this does not guarantee perfect retention.

Local evidence is saved under `.compass/assisted/`. Ordinary final native usage
includes model-visible reminder context and its later effects; do not add hook
request usage to that total a second time. CLI0.171.0's final aggregate was
observed to exclude a separate model request used for context compaction.
An observed compaction, missing diagnostic counters, or a reconciliation mismatch
therefore makes `usage_complete` false. Raw final counters and the separately
labeled diagnostic tally remain available; neither is advertised as a complete
price in that case. The launcher does not silently merge or double-count them.

A real CLI0.171.0 root-session manual compaction followed by another model request
confirmed delivery: the model quoted the short compaction anchor without reading
files. This verifies that delivery path, not a general quality improvement or
perfect retention after every future compaction.
