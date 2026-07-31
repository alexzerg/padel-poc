# Security

Please do not report credentials, API keys or private deployment details in public issues.
Contact the repository owner privately with a minimal reproduction.

Before publishing or deploying:

1. Scan the working tree for tokens and private endpoints.
2. Use Kubernetes Secrets or another secret manager for Playtomic and routing credentials.
3. Keep ingress disabled or protected until authentication and rate limiting are configured.
4. Treat `/api/ingest` as a trusted internal endpoint.
5. Rotate any key that has appeared in shell output, logs, screenshots or chat.
