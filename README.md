# Neuron: work built during the Meta Global AI Developer Hackathon

Neuron is a workspace where you run a project with a team of AI bots. You describe what you want. A Master bot sets up the team, plans the work with you, builds the first version in a sandbox, and keeps the team working after that. People stay in the loop: anything that leaves the app, such as mail, a publish or a standing routine, waits behind an Allow card until a person says yes.

The live product is at https://neuronbot.in. That is what the hackathon judges assess. This repository is not a runnable copy of it.

## Contributors

Tanay Kapoor and Ayushi Gupta.

## What is in this repo

Only the work done between Oct 27 and Nov 4, 2026, and nothing else.

- **New files, in full**, at the same paths they have in the product (for example `backend/services/sites.py`, `backend/agents/versions.py`).
- **Changes to existing files, as diffs**, in `changes-to-existing-code/<path>.diff`. Each is a unified diff with no surrounding context lines, against the last commit before the hackathon (`e0703a5`). The existing file itself is not here.
- **`HACKATHON.md`**: the attribution, and one line for every new file and every changed file.

## Pre-existing vs built this week

| | Where it lives |
|---|---|
| Built before the hackathon: the chat app, bots, board, routines, integrations, billing, the existing build and review pipeline | Not in this repo. It is the baseline (`e0703a5`) of the private Neuron repository. |
| Built this week, new files | Copied here in full. |
| Built this week, edits to pre-existing files | Here as diffs only. |

The history of this repo is the real hackathon history: 55 commits and 17 merges after the baseline, with their original dates and branches. No commit holds baseline code.

## How this relates to neuronbot.in

The product at neuronbot.in is the private Neuron repository deployed. The work in this repo is part of it. To see it work, open the live product. The diffs apply on top of the baseline commit of the private repository and are not meant to be applied on their own.

## Meta models

Both are reached through the model proxy (`backend/routes/model_proxy.py`, `MODEL_PROXY_PROVIDER=meta`). The ids are the defaults and can be overridden with `META_MODEL_*`.

- `muse-spark-1.3` (Meta Muse Spark): the fast, workhorse and judgement tiers, at low, medium and high reasoning effort. Muse Spark also writes and tests the first build of a project inside a sandbox.
- `muse-wonder-1.0` (Meta Muse Wonder): the reasoning tier.
- `muse-image-1.0`: the images in a built site, through a small script the build sandbox runs, capped per build.
