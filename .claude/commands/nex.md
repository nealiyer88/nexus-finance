Run the feature pipeline against this repo.

Read FEATURE_QUEUE.md. Process features in order — skip SHIPPED,
execute QUEUED and IN PROGRESS, stop if a dependency is BLOCKED.

For each feature, follow the feature-pipeline skill instructions:
debate → synthesize → build → verify → commit → log.

Start by reading all briefs and confirming the dependency chain.

Process a maximum of 3 features per run, then stop and write to RUN_LOG.md.
