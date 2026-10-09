# Data release

The release contains the paper's step-level safety-review records.

| File | Records | Role |
|---|---:|---|
| `train_7767.json` | 7,767 | In-domain training split |
| `test_1436.json` | 1,436 | In-domain test split |
| `mailu_ood_198.json` | 198 | Held-out Mailu split |

Each file is a JSON array. A record has three top-level fields:

- `prompt`: system and user messages for the safety reviewer.
- `completion`: the target reviewer response.
- `meta`: source, service, task, step, persona, and decision metadata.

The decision label in `meta.decision` is one of:

```text
direct_execute
ask_human
refuse
```

The corresponding completion tokens are:

```text
<|direct_execute|>
<|ask_human|>
<|refuse|>
```

The in-domain release preserves two target formats used during corpus
construction: 8,586 records contain a closed `<think>...</think>` rationale
before the decision token, and 617 records begin directly with the decision
token. All 198 Mailu records use the decision-first format. In both formats,
the decision token is followed by the same JSON payload. New records produced
by the released pipeline use the decision-first format.

The Mailu records are excluded from the 9,203-record in-domain corpus. Counts
and paths are also recorded in `manifest.json`.
