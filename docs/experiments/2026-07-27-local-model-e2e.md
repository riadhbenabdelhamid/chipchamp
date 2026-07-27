# e2e autopsy — 22 run(s)

## feature reached, per model × scenario
| model | cache | disclosure | detach | notebook | spawn | budget |
|---|---|---|---|---|---|---|
| qwen3-coder:30b | no (timeout) | no (timeout) | no (timeout) | – | – | – |
| nemotron-3-nano:4b | no (answered) | YES | YES | YES | YES | YES |
| qwen3.6:35b | – | – | – | – | – | – |
| gpt-oss:20b | no (capped) | YES | no (answered) | YES | YES | YES |
| devstral:24b | no (answered) | no (answered) | YES | no (answered) | YES | no (answered) |

## did the FEATURE work anywhere (chipchamp's property)
- **cache** (job_cache_hit): NOT OBSERVED
- **disclosure** (tools_load_called): YES — gpt-oss:20b, nemotron-3-nano:4b
- **detach** (detached_job): YES — devstral:24b, nemotron-3-nano:4b
- **notebook** (note_recorded): YES — gpt-oss:20b, nemotron-3-nano:4b
- **spawn** (spawn_called): YES — devstral:24b, gpt-oss:20b, nemotron-3-nano:4b
- **budget** (budget_blocked): YES — gpt-oss:20b, nemotron-3-nano:4b

## cost + outcome per run
| model | scenario | outcome | steps | wall s | tokens | ctx peak | compactions |
|---|---|---|---|---|---|---|---|
| qwen3-coder:30b | cache | timeout | 0 | 413.79 | 0 | 142 | 0 |
| qwen3-coder:30b | disclosure | timeout | 0 | 412.05 | 0 | 156 | 0 |
| qwen3-coder:30b | detach | timeout | 0 | 411.35 | 0 | 177 | 0 |
| nemotron-3-nano:4b | cache | answered | 6 | 169.75 | 42466 | 1400 | 0 |
| nemotron-3-nano:4b | disclosure | answered | 7 | 189.11 | 73673 | 1596 | 0 |
| nemotron-3-nano:4b | detach | answered | 10 | 266.71 | 88483 | 2639 | 0 |
| nemotron-3-nano:4b | notebook | answered | 4 | 64.0 | 33065 | 893 | 0 |
| nemotron-3-nano:4b | spawn | answered | 6 | 464.74 | 52254 | 2208 | 0 |
| nemotron-3-nano:4b | budget | answered | 2 | 83.89 | 8414 | 76 | 0 |
| qwen3.6:35b | - | unavailable | - | - | - | - | - |
| gpt-oss:20b | cache | capped | 12 | 155.63 | 69018 | 4084 | 0 |
| gpt-oss:20b | disclosure | completed | 10 | 70.66 | 65043 | 8061 | 0 |
| gpt-oss:20b | detach | answered | 1 | 39.84 | 6069 | 177 | 0 |
| gpt-oss:20b | notebook | answered | 6 | 43.48 | 33386 | 3427 | 0 |
| gpt-oss:20b | spawn | capped | 8 | 102.43 | 41103 | 2796 | 0 |
| gpt-oss:20b | budget | answered | 2 | 18.09 | 5238 | 76 | 0 |
| devstral:24b | cache | answered | 5 | 195.51 | 33047 | 3680 | 0 |
| devstral:24b | disclosure | answered | 2 | 38.5 | 11542 | 315 | 0 |
| devstral:24b | detach | completed | 8 | 112.63 | 50328 | 3374 | 0 |
| devstral:24b | notebook | answered | 1 | 35.41 | 6010 | 156 | 0 |
| devstral:24b | spawn | completed | 6 | 343.29 | 108405 | 9309 | 0 |
| devstral:24b | budget | answered | 1 | 32.88 | 5780 | 76 | 0 |

## tools each model actually reached for
- **qwen3-coder:30b**: (none)
- **nemotron-3-nano:4b**: plan.update×4, sim.run×4, design.hierarchy×2, fs.grep×2, fs.list×2, lint.run×1, tools.load×1, job.status×1, policy.check×1, note.add×1, agent.spawn×1
- **qwen3.6:35b**: (none)
- **gpt-oss:20b**: fs.grep×6, fs.list×5, synth.run×3, sim.list_tests×2, fs.read×2, plan.update×2, agent.spawn×2, fs__search×1, fs_list×1, design.hierarchy×1, design.module×1, report.done×1
- **devstral:24b**: fs.read×4, sim.run×3, fs.list×3, design.hierarchy×2, report.done×2, lint.run×1, job.status×1, agent.spawn×1, design.module×1
