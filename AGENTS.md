# Project instructions

- Keep source code outside OneDrive. Credentials, message exports, reviewed personal examples, databases, models, and backups must stay outside the repository.
- External links in generated application interfaces must use `target="_blank"` and `rel="noopener noreferrer"` unless explicitly requested otherwise.
- Do not use em dashes in writing.
- Do not start, stop, restart, replace, or interfere with a development server without explicit permission for that action in the current request. Existing servers and ports belong to the user.
- Keep bot access limited to configured servers and channels. Owner controls must use numeric Discord identity checks.
- Never log tokens, message bodies, model prompts or replies, or raw exceptions containing these values.
- Use synthetic test data. Never upload personal messages or credentials to GitHub.
- Exclude NSFW channels from personality data. Imports require recent verified channel metadata; unknown age-restriction status must fail closed.
- Keep the dashboard local to loopback. Preserve Host/Origin checks, local API headers, no-store responses, CSP, and content-free activity metrics. Never give the dashboard Docker-socket access or arbitrary SQL execution.

