## Reviewed dev-to-main promotion

Related release readiness: #58 (ASC-63). Do not use this template for a feature PR.
Base **main**, head **AgamMakkar21:dev**. A new dev push invalidates this evidence
and needs fresh checks and independent review.

<!-- asc60-release
{
  "source_commit": "<full current dev commit>",
  "dev_run_id": 0,
  "rollback_run_id": 0,
  "bundle_sha256": "<SHA-256 of the tested artifact's bundle.json>",
  "config_sha256": "<SHA-256 of deploy/production.json>",
  "evaluation_url": "https://github.com/AgamMakkar21/ai-sous-chef/issues/58#issuecomment-<id>"
}
-->

- [ ] Exact-source CI and dev deployment passed; immutable release artifact exists.
- [ ] Dev rollback and restore smoke checks passed for this source.
- [ ] Evaluation/readiness evidence names this source, including unresolved blockers.
- [ ] Production resources, identities, region and costs have owner approval.
- [ ] Actual main rules require all six CI checks and current peer review.
- [ ] Actual prod environment requires owner approval, prevents self-review and allows main only.
- [ ] Data-contract compatibility, artifact retention and recovery limits were reviewed.
- [ ] Another collaborator approved the final head; no self-approval or bypass.

Production deployment is not approved by opening or merging this PR alone.
See [promotion and rollback](https://github.com/AgamMakkar21/ai-sous-chef/blob/dev/docs/production-release.md).
