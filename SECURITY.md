# Security policy

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting: on the
repository's **Security** tab choose **Report a vulnerability**. Include what you found, how to
reproduce it, and the version (`festrush --help` or the release tag).

You can expect an acknowledgement as soon as a maintainer sees it. This is a small project with
no on-call rota, so there is no guaranteed response time.

## Supported versions

Only the latest release and `main`.

## What is in scope

The application in `src/`, the gateway and Compose files. Demo accounts and the default secrets in
`.env.example` are for local use only; running them on a public host is a misconfiguration, not a
vulnerability.

## What the project already does

Hashed passwords, server-side sessions, CSRF tokens, a strict Content-Security-Policy, login
rate limiting, and CodeQL and a dependency audit on every pull request. The findings in the
original version and how each was fixed are in [docs/audit.md](docs/audit.md).
