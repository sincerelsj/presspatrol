# Design decisions

## Use rules as a baseline and models for contextual review

Layouts and field labels vary. Rules provide a repeatable first result; model review handles context that is difficult to express as labels alone. Keeping both results makes disagreement inspectable. It also adds orchestration and provenance work. The extraction layer validates verbatim evidence against its input rather than accepting unsupported model text.

## Capture evidence before deciding; annotate afterward

Evidence should reflect what was collected, while annotations should reflect the final extracted values. Saving original screenshots and rendered HTML before judgment, then generating annotations from saved HTML afterward, separates those needs and reduces repeat visits. Saved HTML can differ from a live page, so evidence gaps must remain explicit.

## Separate unavailability from missing disclosures

Failure to collect a site is not proof that a disclosure is missing. Operating-status checks and failed-site resolution form their own path. Registry mismatches are also tracked separately from disclosure presence. These distinctions prevent infrastructure failures or registration differences from silently becoming missing-field judgments.

## Make human verification part of the system

Automatic findings remain drafts. Assignment scopes, approval dependencies, version conflicts, and event history make review state explicit. A changed earlier stage invalidates later approvals. This is more work than editable output cells, but preserves the relationship between source, decision, and reviewer action.

## Keep the implementation honest

This archive retains an existing script-oriented architecture instead of introducing a new framework for presentation. The UI uses the Python standard library and SQLite. Model selection is configurable; the current SDK integration is Anthropic. Historical source comments may describe older behavior. No accuracy, deployment, cost-reduction, or production-readiness claim is inferred from fixture tests.
