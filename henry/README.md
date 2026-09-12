# Henry appeal triage pipeline

This directory contains Henry's standalone S3/Bedrock prototype. It is kept in
its own namespace so its module names and output files do not collide with the
repository's `core/` pipeline.

## Setup

From the repository root on Windows PowerShell:

```powershell
.\henry\setup_and_run.ps1
```

Or create a virtual environment and install `../requirements.txt`, then run:

```powershell
python .\henry\workflow_appeal.py TEST001
```

AWS credentials are read by boto3's standard credential chain. Do not commit
credentials. These environment variables can override deployment-specific
settings:

| Variable | Default |
|---|---|
| `AWS_REGION` | `us-west-2` |
| `APPEAL_S3_BUCKET` | `bucket-jahseh` |
| `APPEAL_PDF_PREFIX` | `原始訴願書v2_pdf/` |
| `APPEAL_TEST_PDF_PREFIX` | `測試訴願書_pdf/` |
| `APPEAL_ANSWER_KEY` | `appeals_structured.jsonl` |
| `BEDROCK_MODEL_LIGHT` | `amazon.nova-lite-v1:0` |
| `BEDROCK_MODEL_WRITER` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |

Bedrock calls in `rag_triage.py` are serialized with a 1.05-second minimum
interval to satisfy the project's process-wide maximum of one request per
second. Generated reports, predictions, and `output_json/` are ignored by Git.

## Included files

- `workflow_appeal.py`: end-to-end workflow and JSON output
- `rag_triage.py`: RAG retrieval and model invocation
- `pipeline_two_stage.py`: Nova first pass and Claude review
- `triage*.py`: earlier baselines retained for comparison
- `kb_index.json`: prebuilt public-law embedding index used by the workflow
- `create_bucket.py`, `verify_sts.py`: AWS setup helpers
