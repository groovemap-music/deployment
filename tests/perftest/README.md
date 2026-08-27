# Catalog API performance deployment

The performance-runner source and image are owned and versioned by
[`groovemap-music/catalog-api`](https://github.com/groovemap-music/catalog-api) under its
`performance/` directory. This deployment repository owns only environment-specific
configuration and invocation.

Set `PERFTEST_IMAGE` to an approved immutable image reference containing `@sha256:`, set
`PERFTEST_NETWORK` if the Compose network is not `deployment_discogsography`, and run:

```bash
PERFTEST_IMAGE='ghcr.io/groovemap-music/catalog-api-performance@sha256:<digest>' \
  just performance
```

The recipe starts a workload against a running environment and therefore requires
operator approval. Results are written to the ignored `perftest-results/` directory.
Nothing in `just check` or CI starts the workload.
