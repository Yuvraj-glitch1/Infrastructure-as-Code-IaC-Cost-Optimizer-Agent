# Infrastructure-as-Code (IaC) Cost Optimizer Agent

A CI/CD agent that intercepts unoptimized Terraform in a pull request, prices it with
Infracost, asks an LLM for a cheaper architectural equivalent, rewrites the `.tf` files
in place, and pushes the optimized code back onto the PR branch.

```
Infrastructure-as-Code-IaC-Cost-Optimizer-Agent/
├── .github/
│   └── workflows/
│       └── ai-cost-optimizer.yml   # PR-triggered pipeline
├── terraform/
│   ├── main.tf                     # Baseline over-provisioned AWS stack
│   └── variables.tf                # Inputs (oversized defaults)
├── optimizer.py                    # The agent: Infracost -> LLM -> file rewrite
├── requirements.txt
├── .gitignore
└── README.md
```

## How it works

1. **Trigger** — a PR touching any `*.tf` file starts the workflow.
2. **Price** — `infracost breakdown --format json` produces a machine-readable cost model.
3. **Parse** — `optimizer.py` flattens the JSON into a total monthly cost plus ranked
   cost drivers (including nested subresources such as root block devices).
4. **Prompt** — the raw HCL and the cost report are injected into a strict system prompt
   that forbids re-architecting and scales aggressiveness by `var.environment`.
5. **Rewrite** — the model returns a JSON object of complete file contents; the agent
   validates it, backs up the originals, writes the new files, and runs `terraform fmt`
   as a syntax gate. A parse failure rolls everything back.
6. **Commit** — the workflow detects the dirty tree, commits with `[skip ci]`, and pushes
   to the PR head branch, then comments the estimated savings.

## Local usage

```bash
pip install -r requirements.txt
export INFRACOST_API_KEY=ico-xxxx
export ANTHROPIC_API_KEY=sk-ant-xxxx

# See proposed changes without touching disk
python optimizer.py --terraform-dir terraform --dry-run --verbose

# Apply them
python optimizer.py --terraform-dir terraform
```

Flags: `--provider {anthropic,openai}`, `--model`, `--no-diff`, `--dry-run`, `--verbose`.

## Safety notes

- The agent never renames resources (that would force destroy/create).
- `prod` environments get conservative changes only; Multi-AZ, replicas, and backups
  are left alone.
- Every run backs up originals to `*.pre-optimizer.bak` and rolls back on a failed
  `terraform fmt`. Backups are gitignored and cleaned up on success.
- Always review the bot's commit before merging.
