# API agent catalog

These agents are script-invoked model tasks with prompts, tool schemas, and post-processing. They are not conversational assistants browsing sites on behalf of the pipeline, and do not imply a multi-agent orchestration framework.

1. [Text reviewer](text-review/README.md): extracts and compares disclosure findings from saved text.
2. [Vision diagnostician](vision-diagnosis/README.md): diagnoses image/DOM gaps from saved screenshots.
3. [Publication-date verifier](publication-date/README.md): confirms date evidence and saved article context.
4. [Failed-site resolver](site-resolution/README.md): searches for candidate sites, then checks registered identity information.
5. [I-3 site classifier](site-classification/README.md): classifies non-news screens from saved text.
6. [Run inspector](run-inspection/README.md): interprets run-level diagnostic dimensions.

Each guide links directly to its implementation. Existing source filenames are retained in [scripts/](../scripts/README.md); no prompts or decision logic are hidden in this documentation folder.

All use the current Anthropic SDK integration and shared model/key configuration. Sequential calls are the intended workflow. See [setup](../SETUP.md) and [cost accounting](../COSTS.md). Offline checks do not call the model. ASCII tool-schema property keys are mapped to Korean output fields in code.
