# Alex: Multi-Agent AI Financial Planner on AWS

**Alex** (Agentic Learning Equities eXplainer) is a SaaS app that analyses a user's investment portfolio. You sign in, enter your accounts and holdings, and a team of AI agents writes a portfolio report, draws allocation charts and projects your retirement outcomes. Everything runs serverless on AWS and is defined in Terraform.

> **Origin:** Alex started as the capstone project of Ed Donner's *AI in Production* course. I deployed the full system to my own AWS account, debugged it end to end, and have kept extending it. My changes are listed in [What I added](#what-i-added) and are visible in the commit history.

<!-- TODO: add a demo video link and screenshots (app, Langfuse trace, CloudWatch dashboard) to assets/ -->

## Architecture

```mermaid
graph LR
    User((User)) --> CF[CloudFront + S3<br/>Next.js frontend]
    CF --> APIGW[API Gateway]
    APIGW --> API[Lambda<br/>FastAPI backend]
    API --> DB[(Aurora Serverless v2<br/>PostgreSQL, Data API)]
    API --> SQS[SQS job queue]
    SQS --> Planner[Planner<br/>orchestrator agent]
    Planner --> Tagger[Tagger agent]
    Planner --> Reporter[Reporter agent]
    Planner --> Charter[Charter agent]
    Planner --> Retirement[Retirement agent]
    Reporter --> Vectors[(S3 Vectors<br/>research knowledge base)]
    Tagger & Reporter & Charter & Retirement --> DB
    Researcher[Researcher agent<br/>Lambda + Playwright MCP] --> Ingest[Ingest Lambda]
    Ingest --> SM[SageMaker Serverless<br/>embeddings]
    Ingest --> Vectors
    Sched[EventBridge] --> Researcher
```

| Agent | Role |
|---|---|
| **Planner** | Orchestrator: reads the job from SQS, decides which specialists to call, and invokes them in parallel |
| **Tagger** | Classifies instruments (ETFs, stocks) by asset class, region and sector |
| **Reporter** | Writes the portfolio analysis using research retrieved from S3 Vectors, then scores its own output with an LLM-as-judge |
| **Charter** | Produces chart data for the frontend visualisations |
| **Retirement** | Runs retirement projections from the user's goals and holdings |
| **Researcher** | Browses the web on a schedule and adds market research to the knowledge base |

## Tech stack

- **AI:** OpenAI Agents SDK, AWS Bedrock (Amazon Nova Pro, GPT-OSS 120B) via LiteLLM, SageMaker Serverless embeddings (all-MiniLM-L6-v2), Playwright MCP
- **Backend:** Python 3.12 with uv, FastAPI, AWS Lambda, SQS, Aurora Serverless v2 PostgreSQL (Data API), S3 Vectors
- **Frontend:** Next.js (Pages Router), React, Clerk authentication, CloudFront
- **Infrastructure:** Terraform (one independent stack per layer), Docker-built Lambda packages, ECR
- **Operations:** Langfuse tracing, CloudWatch dashboards and alarms, LLM-as-judge evaluation

## Design decisions

- **S3 Vectors instead of OpenSearch** for the knowledge base: about 90% cheaper at this scale, with no cluster to run.
- **Serverless everywhere** (Lambda, Aurora Serverless v2, SageMaker Serverless), so the system costs close to nothing when idle. The whole stack can be torn down and redeployed layer by layer.
- **SQS between the API and the agents**, so a long multi-agent run never holds an HTTP request open, and failed jobs can be retried.
- **Aurora Data API** instead of VPC networking, so Lambdas reach the database over HTTPS with IAM and Secrets Manager.
- **One Terraform stack per layer** (`terraform/2_sagemaker` … `terraform/8_enterprise`), so each part can be deployed, debugged and destroyed independently.
- **Each agent uses either structured outputs or tools, never both**, to work around a current LiteLLM/Bedrock limitation.

## What I added

- **Langfuse SDK upgraded from v3 to v4 in all five agents** ([commit](https://github.com/ek-neize/alex-ai-financial-planner/commit/9932b39)). I found and fixed two problems on the way:
  - logfire 4.41 and later imports `TaskSpanData`, which doesn't exist in the openai-agents version in use, so I pinned logfire to 4.7.0.
  - Langfuse v4 only exports LLM-shaped spans by default, which silently dropped the agent and tool spans. A custom `should_export_span` filter restores the full trace tree.

  Traces now arrive in near real time instead of after a ~15 minute delay.
- **Fixed Lambda packaging on Linux** ([commit](https://github.com/ek-neize/alex-ai-financial-planner/commit/447d20e)): the Docker build left root-owned files that broke later runs.
- **Made the Bedrock region consistent** across agents, Terraform and config, and resized the researcher Lambda so its headless browser runs reliably.
- Diagnosed production issues across the stack: Bedrock region and model access, Terraform variables that were silently ignored, and missing database migrations after recreating Aurora.

## Roadmap

- [ ] Terraform remote state in S3 with locking
- [ ] CI/CD with GitHub Actions: tests on every PR, automatic Lambda deploys on merge
- [ ] Database migrations run automatically as part of deployment
- [ ] Load testing for the agent pipeline

## Running it yourself

You need an AWS account with Bedrock model access, Docker, Terraform, uv, Node.js and a Clerk account. The step-by-step deployment guides are in [`guides/`](guides/), starting with [`1_permissions.md`](guides/1_permissions.md). Each `terraform/` directory has a `terraform.tfvars.example` to copy and fill in.

**Cost:** while it's deployed, Aurora is the main cost. Run `terraform destroy` in each directory, from 8 down to 2, when you're not using it.

## Credits

Original course material and codebase by [Ed Donner](https://github.com/ed-donner/alex), MIT licensed. See [LICENSE](LICENSE).
